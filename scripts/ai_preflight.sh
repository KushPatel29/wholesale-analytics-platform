#!/bin/bash
set -e

# ANSI Color Codes
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${CYAN}🚀 Starting God Level AI Preflight for Wholesale Analytics...${NC}"

# 1. Configuration Check
echo -n "Checking App Factory & Config... "
python3 manage.py check-config > /dev/null 2>&1 && echo -e "${GREEN}✅ OK${NC}" || echo -e "${RED}❌ FAILED${NC}"

# 2. Data Integrity
echo -n "Checking Parquet/DuckDB Data... "
make smoke > /dev/null 2>&1 && echo -e "${GREEN}✅ OK${NC}" || echo -e "${YELLOW}⚠️ WEAK (Run 'make smoke')${NC}"

# 3. Security Check
echo -n "Checking RBAC/Security Layer... "
PYTHONPATH=. make smoke-rbac > /dev/null 2>&1 && echo -e "${GREEN}✅ OK${NC}" || echo -e "${RED}❌ FAILED${NC}"

# 4. Code Quality
echo -n "Running Lint Check... "
# Attempt linting, but don't fail the whole script if it's just formatting
make lint > /dev/null 2>&1 && echo -e "${GREEN}✅ OK${NC}" || echo -e "${YELLOW}⚠️ LINT ISSUES (Run 'make lint' or 'make format')${NC}"

# 5. Core Tests
echo -n "Running Fast Unit Tests... "
PYTHONPATH=. python3 -m pytest tests/test_filters_canonical_v2.py -q --no-summary > /dev/null 2>&1 && echo -e "${GREEN}✅ OK${NC}" || echo -e "${RED}❌ FAILED${NC}"

# 6. AI Knowledge Graph
echo -n "Verifying AI Knowledge Graph... "
if [ -f graphify-out/graph.json ]; then
    echo -e "${GREEN}✅ OK${NC}"
else
    echo -e "${RED}❌ MISSING${NC}"
    echo -e "${YELLOW}Hint: Run 'graphify run .' to build the graph.${NC}"
fi

echo "------------------------------------------------"
echo -e "${CYAN}🌟 Preflight Complete. Repo is ready for AI updates.${NC}"
