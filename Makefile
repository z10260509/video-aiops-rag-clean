.PHONY: aiops-status aiops-ui aiops-test aiops-security aiops-model-check \
        aiops-video-check aiops-fault-codec aiops-benchmark aiops-browser aiops-migrations
aiops-status:
	kubectl -n aiops get deployments,pods,services
aiops-ui:
	kubectl -n video-platform port-forward --address 127.0.0.1 service/frontend 13010:80
aiops-test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:. REMEDIATION_ENABLED=false \
		python -m pytest -q -p no:cacheprovider \
		backend/tests worker/tests tests/integration
.PHONY: aiops-test-docker
aiops-test-docker:
	docker build --target test -t video-platform-backend-tests:local -f backend/Dockerfile .
	docker run --rm --network none -e PYTHONPATH=/app/backend:/app -e REMEDIATION_ENABLED=false -w /app \
		-v "$(CURDIR)/backend:/app/backend:ro" -v "$(CURDIR)/worker:/app/worker:ro" \
		-v "$(CURDIR)/scripts:/app/scripts:ro" -v "$(CURDIR)/k8s:/app/k8s:ro" \
		-v "$(CURDIR)/tests:/app/tests:ro" video-platform-backend-tests:local \
		python -m pytest -q -p no:cacheprovider backend/tests worker/tests tests/integration
aiops-security:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:. python -m pytest -q \
		backend/tests/test_aiops_actions.py backend/tests/test_hypothesis_v2.py \
		backend/tests/test_video_integration.py backend/tests/test_project_tools.py
aiops-model-check:
	PYTHONPATH=backend:. python scripts/validate_hypothesis_v2.py
aiops-video-check:
	@test -n "$(VIDEO)" -a -n "$(OUTPUT)" || (echo 'Set VIDEO=path.mp4 OUTPUT=new-result.json'; exit 2)
	python scripts/run_video_scenario.py "$(VIDEO)" --output "$(OUTPUT)"
aiops-fault-codec:
	@test -n "$(VIDEO)" -a -n "$(OUTPUT)" || (echo 'Set VIDEO=path.mp4 OUTPUT=new-result.json'; exit 2)
	python scripts/run_video_scenario.py "$(VIDEO)" --fault --output "$(OUTPUT)"
aiops-benchmark:
	@test -n "$(VIDEO)" -a -n "$(OUTPUT)" || (echo 'Set VIDEO=path.mp4 OUTPUT=new-result.json'; exit 2)
	python scripts/benchmark_aiops.py "$(VIDEO)" \
		--output "$(OUTPUT)" --repeat $(or $(REPEAT),1)
aiops-browser:
	node tests/browser/aiops.cjs
aiops-migrations:
	@test -n "$(OUTPUT)" || (echo 'Set OUTPUT=new-result.json'; exit 2)
	python scripts/verify_migrations.py --output "$(OUTPUT)"
.PHONY: k8s-check
k8s-check:
	python scripts/validate_k8s.py

.PHONY: format format-check
format:
	python3 -m ruff format backend worker scripts tests
	frontend/node_modules/.bin/prettier --write . --ignore-unknown
	shfmt -i 2 -ci -sr -w scripts/*.sh
format-check:
	python3 -m ruff format --check backend worker scripts tests
	frontend/node_modules/.bin/prettier --check . --ignore-unknown
	shfmt -i 2 -ci -sr -d scripts/*.sh
