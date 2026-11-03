package main

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"testing"
)

// The matrix is shared with the Python side (tests/test_guardian_contract.py): this test pins what guardiand does
// with every (exit code, reply) pair, so a function and the middleware cannot drift apart unnoticed.
func TestContractMatrixMatchesDecodeResult(t *testing.T) {
	raw, err := os.ReadFile("../../src/engine_core/guardian/contract_matrix.json")
	if err != nil {
		t.Fatal(err)
	}
	var matrix struct {
		Rows []struct {
			ID     string          `json:"id"`
			Exit   int             `json:"exit"`
			Reply  json.RawMessage `json:"reply"`
			Expect struct {
				Status string `json:"status"`
				Code   string `json:"code"`
				Retry  bool   `json:"retry"`
			} `json:"expect"`
		} `json:"rows"`
	}
	if err := json.Unmarshal(raw, &matrix); err != nil {
		t.Fatal(err)
	}
	if len(matrix.Rows) == 0 {
		t.Fatal("empty contract matrix")
	}
	for _, row := range matrix.Rows {
		var out []byte
		if string(row.Reply) != "null" {
			out = append([]byte(nil), row.Reply...)
			out = append(out, '\n')
		}
		var runErr error
		if row.Exit != 0 {
			var ee *exec.ExitError
			runErr = exec.Command("sh", "-c", "exit "+strconv.Itoa(row.Exit)).Run()
			if !errors.As(runErr, &ee) {
				t.Fatalf("%s: could not produce exit %d", row.ID, row.Exit)
			}
		}
		status, code, retry, _ := decodeResult(out, runErr)
		if status != row.Expect.Status || code != row.Expect.Code || retry != row.Expect.Retry {
			t.Errorf("%s: decodeResult(exit %d) = (%s, %s, %v), matrix expects (%s, %s, %v)", row.ID, row.Exit,
				status, code, retry, row.Expect.Status, row.Expect.Code, row.Expect.Retry)
		}
	}
}
