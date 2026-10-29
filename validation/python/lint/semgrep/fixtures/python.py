"""Fixtures for semgrep/python.yml (vendored upstream pack):
positive and negative cases per rule, one section per rule id."""

import subprocess

import httpx
import requests

# --- kit.python.no-hardcoded-secrets ---

# ruleid: kit.python.no-hardcoded-secrets
GITHUB_TOKEN = "ghp_1234567890abcdef1234567890abcdef1234"

# ruleid: kit.python.no-hardcoded-secrets
AWS_ACCESS_KEY = "AKIAABCDEFGHIJKLMNOP"

# ok: kit.python.no-hardcoded-secrets
GITHUB_TOKEN_REF = settings.github_token


# --- kit.python.no-dynamic-code-execution ---

# ruleid: kit.python.no-dynamic-code-execution
eval(user_supplied_expression)

# ruleid: kit.python.no-dynamic-code-execution
exec(generated_source)

# ok: kit.python.no-dynamic-code-execution
result = ast.literal_eval(user_supplied_expression)


# --- kit.python.no-subprocess-shell ---

# ruleid: kit.python.no-subprocess-shell
subprocess.run(command_string, shell=True)

# ruleid: kit.python.no-subprocess-shell
subprocess.check_output(command_string, shell=True)

# ok: kit.python.no-subprocess-shell
subprocess.run(["ls", "-la"], shell=False)


# --- kit.python.no-disabled-tls-validation ---

# ruleid: kit.python.no-disabled-tls-validation
requests.get(url, verify=False)

# ruleid: kit.python.no-disabled-tls-validation
httpx.post(url, json=payload, verify=False)

# ok: kit.python.no-disabled-tls-validation
httpx.post(url, json=payload, verify=True)
