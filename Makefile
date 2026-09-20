.PHONY: test build check test-ui

test:
	cd plugin && go test -race ./...
	python3 -m unittest discover -s tests -v

build:
	cd plugin && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o state-reuse .
	cd plugin && go run ./cmd/pack

check:
	python3 -m compileall -q collector monitor tests
	node --check monitor/app.js
	node --check monitor/entry.js

# Provide jsdom through NODE_PATH when running this standalone monitor suite.
test-ui:
	node tests/monitor_ui.test.cjs
