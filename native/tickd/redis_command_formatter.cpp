#include "redis_command_formatter.h"

#include <utility>

namespace tickd {

namespace {

const char* kAtomicCutoffPublishScript =
    "local payload_exists=redis.call('EXISTS',KEYS[1]); "
    "local meta_exists=redis.call('EXISTS',KEYS[2]); "
    "if payload_exists~=meta_exists then "
    "return redis.error_reply('atomic cutoff publish partial existing keys') end "
    "if payload_exists==1 then return 0 end "
    "redis.call('SET',KEYS[1],ARGV[1]) "
    "local i=3; while i<=#ARGV do redis.call('HSET',KEYS[2],ARGV[i],ARGV[i+1]); i=i+2; end "
    "redis.call('EXPIRE',KEYS[1],ARGV[2]) "
    "redis.call('EXPIRE',KEYS[2],ARGV[2]) return 1";

}  // namespace

bool RedisCommandFormatter::format(const RedisCommand& command, RedisArgvCommand& out, std::string* error) {
    out.argv.clear();
    switch (command.type) {
        case RedisCommandType::HSetMulti:
            return format_hset_multi(command, out, error);
        case RedisCommandType::SetString:
            return format_set_string(command, out, error);
        case RedisCommandType::SAdd:
            return format_sadd(command, out, error);
        case RedisCommandType::Expire:
            return format_expire(command, out, error);
        case RedisCommandType::AtomicCutoffPublish:
            return format_atomic_cutoff_publish(command, out, error);
    }
    if (error) {
        *error = "unknown redis command type";
    }
    return false;
}

RedisCommandFormatStats RedisCommandFormatter::estimate(const std::vector<RedisCommand>& commands) {
    RedisCommandFormatStats stats;
    stats.command_count = commands.size();
    for (const RedisCommand& command : commands) {
        RedisArgvCommand argv;
        std::string error;
        if (!format(command, argv, &error)) {
            stats.ok = false;
            stats.error = error;
            return stats;
        }
        stats.argv_count += argv.argv.size();
        if (command.type == RedisCommandType::HSetMulti ||
            command.type == RedisCommandType::AtomicCutoffPublish) {
            stats.field_count += command.fields.size();
        }
        stats.payload_bytes += command_payload_bytes(argv);
    }
    return stats;
}

bool RedisCommandFormatter::format_atomic_cutoff_publish(
    const RedisCommand& command,
    RedisArgvCommand& out,
    std::string* error
) {
    if (command.key.empty()) {
        if (error) *error = "atomic cutoff payload key is empty";
        return false;
    }
    if (command.secondary_key.empty()) {
        if (error) *error = "atomic cutoff metadata key is empty";
        return false;
    }
    if (command.value.empty()) {
        if (error) *error = "atomic cutoff payload is empty";
        return false;
    }
    if (command.fields.empty()) {
        if (error) *error = "atomic cutoff metadata fields are empty";
        return false;
    }
    if (command.ttl_seconds <= 0) {
        if (error) *error = "atomic cutoff ttl must be positive";
        return false;
    }
    out.argv.reserve(8 + command.fields.size() * 2);
    out.argv.emplace_back("EVAL");
    out.argv.emplace_back(kAtomicCutoffPublishScript);
    out.argv.emplace_back("2");
    out.argv.emplace_back(command.key);
    out.argv.emplace_back(command.secondary_key);
    out.argv.emplace_back(command.value);
    out.argv.emplace_back(std::to_string(command.ttl_seconds));
    for (const auto& field : command.fields) {
        if (field.first.empty()) {
            if (error) *error = "atomic cutoff metadata field name is empty";
            out.argv.clear();
            return false;
        }
        out.argv.emplace_back(field.first);
        out.argv.emplace_back(field.second);
    }
    return true;
}

bool RedisCommandFormatter::format_hset_multi(const RedisCommand& command, RedisArgvCommand& out, std::string* error) {
    if (command.key.empty()) {
        if (error) *error = "HSET key is empty";
        return false;
    }
    if (command.fields.empty()) {
        if (error) *error = "HSET fields are empty";
        return false;
    }
    out.argv.reserve(2 + command.fields.size() * 2);
    out.argv.emplace_back("HSET");
    out.argv.emplace_back(command.key);
    for (const auto& field : command.fields) {
        if (field.first.empty()) {
            if (error) *error = "HSET field name is empty";
            out.argv.clear();
            return false;
        }
        out.argv.emplace_back(field.first);
        out.argv.emplace_back(field.second);
    }
    return true;
}

bool RedisCommandFormatter::format_set_string(const RedisCommand& command, RedisArgvCommand& out, std::string* error) {
    if (command.key.empty()) {
        if (error) *error = "SET key is empty";
        return false;
    }
    out.argv.reserve(3);
    out.argv.emplace_back("SET");
    out.argv.emplace_back(command.key);
    out.argv.emplace_back(command.value);
    return true;
}

bool RedisCommandFormatter::format_sadd(const RedisCommand& command, RedisArgvCommand& out, std::string* error) {
    if (command.key.empty()) {
        if (error) *error = "SADD key is empty";
        return false;
    }
    if (command.members.empty()) {
        if (error) *error = "SADD members are empty";
        return false;
    }
    out.argv.reserve(2 + command.members.size());
    out.argv.emplace_back("SADD");
    out.argv.emplace_back(command.key);
    for (const std::string& member : command.members) {
        if (member.empty()) {
            if (error) *error = "SADD member is empty";
            out.argv.clear();
            return false;
        }
        out.argv.emplace_back(member);
    }
    return true;
}

bool RedisCommandFormatter::format_expire(const RedisCommand& command, RedisArgvCommand& out, std::string* error) {
    if (command.key.empty()) {
        if (error) *error = "EXPIRE key is empty";
        return false;
    }
    if (command.ttl_seconds <= 0) {
        if (error) *error = "EXPIRE ttl must be positive";
        return false;
    }
    out.argv.reserve(3);
    out.argv.emplace_back("EXPIRE");
    out.argv.emplace_back(command.key);
    out.argv.emplace_back(std::to_string(command.ttl_seconds));
    return true;
}

std::size_t RedisCommandFormatter::command_payload_bytes(const RedisArgvCommand& command) {
    std::size_t total = 0;
    for (const std::string& arg : command.argv) {
        total += arg.size();
    }
    return total;
}

}  // namespace tickd
