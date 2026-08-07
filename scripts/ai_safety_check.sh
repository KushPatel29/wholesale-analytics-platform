#!/bin/bash
# 🛡️ God Level AI Safety & Integrity Audit (V3 - Final)

echo "🔍 Running AI-Native Safety Audit..."

# 1. Check for Hardcoded Secrets in Config
echo -n "Checking for hardcoded SECRET_KEY... "
grep -q "SECRET_KEY: str = os.getenv(\"SECRET_KEY\", \"\")" app/config.py && echo "✅ PASS (Empty default forces env)" || echo "❌ FAIL"

# 2. Check for Auth Bypasses
echo -n "Checking for AUTHZ_DISABLED leaks in blueprints... "
grep -r "AUTHZ_DISABLED" app/blueprints | grep -v "_cfg_flag" && echo "❌ FAIL (Raw bypass found)" || echo "✅ PASS"

# 3. Check for Sensitive Logging
echo -n "Checking for PII/Sensitive data logging... "
# Focus on actual password/token logging, not just the word 'token' in an error name
grep -r "logger.*{.*token.*}" app/ && echo "❌ FAIL (Token data in log extra)" || echo "✅ PASS"

# 4. Check for DuckDB Query Injection
echo -n "Checking for raw DuckDB string interpolation... "
grep -r "conn.execute(f\"SELECT.*{.*}\")" app/services/fact_store.py && echo "❌ FAIL (Possible SQL Injection)" || echo "✅ PASS"

echo "------------------------------------------------"
echo "🌟 Safety Audit Complete."
