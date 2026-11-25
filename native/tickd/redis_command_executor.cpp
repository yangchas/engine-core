#include "redis_command_executor.h"

#if defined(T1_V2_ENABLE_REDIS)

#include <sys/time.h>

#include <vector>
#include <regex>

namespace t1_v2 {

namespace {

timeval ms_to_timeval(int ms) {
    timeval tv{};
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (ms % 1000) * 1000;
    return tv;
}

}  // namespace

HiredisRedisCommandExecutor::HiredisRedisCommandExecutor(const ConfigV2& config) : config_(config) {}

HiredisRedisCommandExecutor::~HiredisRedisCommandExecutor() {
    disconnect();
}

RedisExecutionResult HiredisRedisCommandExecutor::preflight() {
    RedisExecutionResult result;
    if (!connect(result)) {
        return result;
    }
    redisReply* reply = static_cast<redisReply*>(redisCommand(context_, "PING"));
    if (!reply) {
        result.ok = false;
        result.error = context_ && context_->errstr ? context_->errstr : "redis ping failed";
        disconnect();
        return result;
    }
    const bool ok = reply->type == REDIS_REPLY_STATUS && reply->str && std::string(reply->str) == "PONG";
    if (!ok) {
        result.ok = false;
        result.error = reply->str ? reply->str : "redis ping failed";
    }
    freeReplyObject(reply);
    if (!ok) {
        disconnect();
        return result;
    }
    result.ok = true;
    return result;
}

RedisExecutionResult HiredisRedisCommandExecutor::execute(const std::vector<RedisCommand>& commands) {
    RedisExecutionResult result;
    if (commands.empty()) {
        result.ok = true;
        result.command_count = 0;
        return result;
    }
    if (!connect(result)) {
        return result;
    }

    int appended = 0;
    int pending = 0;
    for (const RedisCommand& command : commands) {
        if (command.type == RedisCommandType::SetString && !command.secondary_key.empty() && command.expire_at > 0) {
            if (pending && !drain(pending, result)) { disconnect(); return result; }
            pending = 0;
            if (!publish_full(command, result)) { disconnect(); return result; }
        } else {
            if (!append(command, result)) { disconnect(); return result; }
            ++pending;
        }
        ++appended;
    }
    if (pending && !drain(pending, result)) { disconnect(); return result; }
    result.ok = true;
    result.command_count = appended;
    return result;
}

// Payload and metadata each have expiry in their own atomic SET. Mark any
// previous metadata incomplete before replacing the payload so a failure or
// process exit cannot expose an old COMPLETE marker for new data.
bool HiredisRedisCommandExecutor::publish_full(const RedisCommand& command, RedisExecutionResult& result) {
    if (command.fields.size() != 2 || command.fields[0].first != "rows" || command.fields[1].first != "sha256" ||
        command.key.empty() || command.secondary_key.empty() || command.value.empty()) {
        result.ok = false; result.error = "invalid full auction publication"; return false;
    }
    auto run = [&](const std::vector<std::string>& args) -> redisReply* {
        std::vector<const char*> argv; std::vector<std::size_t> lengths;
        for (const auto& arg : args) { argv.push_back(arg.data()); lengths.push_back(arg.size()); }
        return static_cast<redisReply*>(redisCommandArgv(context_, static_cast<int>(argv.size()), argv.data(), lengths.data()));
    };
    auto set_exat = [&](const std::string& key, const std::string& value) -> bool {
        redisReply* reply = run({"SET", key, value, "EXAT", std::to_string(command.expire_at)});
        const bool ok = reply && reply->type == REDIS_REPLY_STATUS && reply->str && std::string(reply->str) == "OK";
        if (!ok) result.error = reply && reply->str ? reply->str : "full auction SET EXAT failed";
        if (reply) freeReplyObject(reply);
        return ok;
    };
    const std::string incomplete = "{\"status\":\"INCOMPLETE\"}";
    if (!set_exat(command.secondary_key, incomplete) || !set_exat(command.key, command.value)) {
        result.ok = false; return false;
    }
    redisReply* reply = run({"GET", command.key});
    if (!reply || reply->type != REDIS_REPLY_STRING) {
        result.ok = false; result.error = reply && reply->str ? reply->str : "full auction GET failed";
        if (reply) freeReplyObject(reply);
        return false;
    }
    const std::string value(reply->str, reply->len);
    freeReplyObject(reply);
    const std::regex symbol_re(R"re("[0-9]{6}":\{)re");
    const auto rows = std::distance(std::sregex_iterator(value.begin(), value.end(), symbol_re), std::sregex_iterator());
    if (value != command.value || std::to_string(rows) != command.fields[0].second) {
        reply = run({"DEL", command.key});
        if (reply) freeReplyObject(reply);
        result.ok = false; result.error = "full auction GET verification mismatch"; return false;
    }
    const std::string complete = "{\"status\":\"COMPLETE\",\"rows\":" + command.fields[0].second +
        ",\"sha256\":\"" + command.fields[1].second + "\"}";
    if (!set_exat(command.secondary_key, complete)) { result.ok = false; return false; }
    return true;
}

void HiredisRedisCommandExecutor::disconnect() {
    if (context_) {
        redisFree(context_);
        context_ = nullptr;
    }
}

bool HiredisRedisCommandExecutor::connect(RedisExecutionResult& result) {
    if (context_) {
        return true;
    }
    timeval connect_timeout = ms_to_timeval(1000);
    context_ = redisConnectWithTimeout(config_.redis.host.c_str(), config_.redis.port, connect_timeout);
    if (!context_ || context_->err) {
        result.ok = false;
        result.error = context_ && context_->errstr ? context_->errstr : "redis connect failed";
        disconnect();
        return false;
    }

    timeval io_timeout = ms_to_timeval(1500);
    if (redisSetTimeout(context_, io_timeout) != REDIS_OK) {
        result.ok = false;
        result.error = "redis set timeout failed";
        disconnect();
        return false;
    }

    if (config_.redis.db > 0) {
        redisReply* reply = static_cast<redisReply*>(redisCommand(context_, "SELECT %d", config_.redis.db));
        if (!reply) {
            result.ok = false;
            result.error = "redis select failed";
            disconnect();
            return false;
        }
        const bool ok = reply->type != REDIS_REPLY_ERROR;
        if (!ok && reply->str) {
            result.error = reply->str;
        }
        freeReplyObject(reply);
        if (!ok) {
            result.ok = false;
            disconnect();
            return false;
        }
    }
    return true;
}

bool HiredisRedisCommandExecutor::append(const RedisCommand& command, RedisExecutionResult& result) {
    RedisArgvCommand formatted;
    std::string error;
    if (!RedisCommandFormatter::format(command, formatted, &error)) {
        result.ok = false;
        result.error = error.empty() ? "redis command format failed" : error;
        return false;
    }
    return append(formatted, result);
}

bool HiredisRedisCommandExecutor::append(const RedisArgvCommand& command, RedisExecutionResult& result) {
    std::vector<const char*> argv;
    std::vector<std::size_t> argvlen;
    argv.reserve(command.argv.size());
    argvlen.reserve(command.argv.size());
    for (const std::string& arg : command.argv) {
        argv.push_back(arg.c_str());
        argvlen.push_back(arg.size());
    }
    if (redisAppendCommandArgv(context_, static_cast<int>(argv.size()), argv.data(), argvlen.data()) != REDIS_OK) {
        result.ok = false;
        result.error = context_ && context_->errstr ? context_->errstr : "redis append failed";
        return false;
    }
    return true;
}

bool HiredisRedisCommandExecutor::drain(int expected_replies, RedisExecutionResult& result) {
    for (int i = 0; i < expected_replies; ++i) {
        void* reply_void = nullptr;
        if (redisGetReply(context_, &reply_void) != REDIS_OK) {
            if (reply_void) {
                freeReplyObject(reply_void);
            }
            result.ok = false;
            result.error = context_ && context_->errstr ? context_->errstr : "redis get reply failed";
            return false;
        }
        auto* reply = static_cast<redisReply*>(reply_void);
        const bool ok = reply && reply->type != REDIS_REPLY_ERROR;
        if (!ok && reply && reply->str) {
            result.error = reply->str;
        }
        if (reply) {
            freeReplyObject(reply);
        }
        if (!ok) {
            result.ok = false;
            return false;
        }
    }
    return true;
}

}  // namespace t1_v2

#endif
