.PHONY: check build
check:
	go vet ./...
	go test ./... -race

build:
	go build -buildvcs=false -o guardiand .
