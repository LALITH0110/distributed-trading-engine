.PHONY: run-local run-local-60 run-local-ml test test-unit test-integration proto clean

PYTHON ?= python3
export PYTHONPATH := $(CURDIR)

# Full local pipeline — runs until Ctrl+C
run-local:
	$(PYTHON) scripts/start_pipeline.py

# 60-second smoke test
run-local-60:
	$(PYTHON) scripts/start_pipeline.py --duration 60

# Full pipeline with ML strategy
run-local-ml:
	$(PYTHON) scripts/start_pipeline.py --with-ml

# All tests
test:
	$(PYTHON) -m pytest tests/ -v --tb=short

# Unit tests only (no ZMQ processes)
test-unit:
	$(PYTHON) -m pytest tests/test_lob.py tests/test_strategy.py tests/test_risk_gateway.py -v --tb=short

# Integration tests (spawns processes)
test-integration:
	$(PYTHON) -m pytest tests/test_matching_engine.py tests/test_strategy_integration.py -v --tb=short

# Compile protobuf
proto:
	bash scripts/compile_proto.sh

# Clean caches
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; true
