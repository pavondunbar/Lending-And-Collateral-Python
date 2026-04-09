.DEFAULT_GOAL := help
COMPOSE       := docker compose
PG_CONN       := postgresql+psycopg2://lending:s3cr3t@localhost:5432/lending_db
TOPIC ?= loan.originated
BOLD  := \033[1m
RESET := \033[0m
CYAN  := \033[36m
GREEN := \033[32m
RED   := \033[31m

.PHONY: help up down down-v build restart logs ps shell-pg shell-kafka \
        test test-unit test-e2e health demo integrity kafka-tail \
        topics seed-accounts \
        db-loans db-collateral db-journal db-balances db-ledger open-docs

help: ## Show this help
	@echo ""
	@echo "$(BOLD)Institutional Lending & Collateral Management$(RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { \
		printf "  $(CYAN)%-22s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

up: ## Build and start all services
	$(COMPOSE) up --build -d
	@echo "$(GREEN)Stack started — run 'make health' to verify.$(RESET)"

down: ## Stop and remove containers (keep volumes)
	$(COMPOSE) down

down-v: ## Stop and remove containers AND volumes
	$(COMPOSE) down -v

build: ## Rebuild all images without starting
	$(COMPOSE) build --no-cache

restart: ## Restart all services
	$(COMPOSE) restart

logs: ## Follow all service logs
	$(COMPOSE) logs -f

ps: ## Container status
	$(COMPOSE) ps

shell-pg: ## psql shell
	$(COMPOSE) exec postgres psql -U lending -d lending_db

shell-kafka: ## kafka bash shell
	$(COMPOSE) exec kafka bash

test: ## Full test suite
	PYTHONPATH=. pytest tests/ -v --tb=short -p no:warnings

test-unit: ## Unit tests (excludes e2e)
	PYTHONPATH=. pytest tests/ -v --tb=short -p no:warnings \
	  --ignore=tests/test_e2e_scenarios.py

test-e2e: ## End-to-end scenario tests
	PYTHONPATH=. pytest tests/test_e2e_scenarios.py -v --tb=short \
	  -p no:warnings

health: ## Check gateway health
	@curl -sf http://localhost:8000/health | python3 -m json.tool || \
	  echo "$(RED)Gateway not reachable$(RESET)"

demo: ## Run live stack demo
	GATEWAY_URL=http://localhost:8000 \
	GATEWAY_API_KEY=$$(grep GATEWAY_API_KEY .env | cut -d= -f2) \
	python3 scripts/demo.py

integrity: ## Ledger double-entry integrity check
	DATABASE_URL=$(PG_CONN) python3 scripts/ledger_integrity.py

kafka-tail: ## Tail a Kafka topic (TOPIC=loan.originated)
	$(COMPOSE) exec kafka kafka-console-consumer \
	  --bootstrap-server localhost:9092 \
	  --topic $(TOPIC) --from-beginning --max-messages 20

topics: ## List Kafka topics
	$(COMPOSE) exec kafka kafka-topics --bootstrap-server localhost:9092 --list

seed-accounts: ## Insert demo accounts
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "INSERT INTO accounts (entity_name,account_type,kyc_verified,aml_cleared,risk_tier) \
	   VALUES ('Demo Bank A','institutional',true,true,1), \
	          ('Demo Bank B','correspondent',true,true,2) \
	   ON CONFLICT DO NOTHING;"
	@echo "$(GREEN)Demo accounts seeded.$(RESET)"

db-loans: ## Show recent loans
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "SELECT loan_ref,currency,principal,interest_rate_bps,status,created_at \
	   FROM loans ORDER BY created_at DESC LIMIT 20;"

db-collateral: ## Show collateral positions
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "SELECT collateral_ref,asset_type,quantity,haircut_pct,status \
	   FROM collateral_positions ORDER BY created_at DESC LIMIT 20;"

db-journal: ## Show recent journal entries
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "SELECT journal_id,coa_code,currency,debit,credit,entry_type,LEFT(narrative,40),created_at \
	   FROM journal_entries ORDER BY created_at DESC LIMIT 20;"

db-balances: ## Account balances (derived from journal)
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "SELECT a.entity_name, je.currency, \
	          ROUND(SUM(je.debit),2) AS total_debit, \
	          ROUND(SUM(je.credit),2) AS total_credit, \
	          ROUND(SUM(je.debit)-SUM(je.credit),2) AS net \
	   FROM journal_entries je \
	   JOIN accounts a ON a.id = je.account_id \
	   GROUP BY a.entity_name, je.currency \
	   ORDER BY a.entity_name;"

db-ledger: ## Trial balance by COA code
	$(COMPOSE) exec postgres psql -U lending -d lending_db -c \
	  "SELECT je.coa_code, c.name, je.currency, \
	          ROUND(SUM(je.debit),2) AS total_debit, \
	          ROUND(SUM(je.credit),2) AS total_credit \
	   FROM journal_entries je \
	   JOIN chart_of_accounts c ON c.code = je.coa_code \
	   GROUP BY je.coa_code, c.name, je.currency \
	   ORDER BY je.coa_code;"

open-docs: ## Open API docs in browser
	open http://localhost:8000/docs 2>/dev/null || xdg-open http://localhost:8000/docs 2>/dev/null || \
	  echo "Visit http://localhost:8000/docs"
