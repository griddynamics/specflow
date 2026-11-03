#!/bin/sh
# The local env file (.env.local): `make env-local` and `make check-keycloak-env` run this script.
#
#   sh scripts/env-local.sh write ENV_FILE            (make env-local)
#   sh scripts/env-local.sh check-keycloak ENV_FILE   (make check-keycloak-env, before make up)
#
# write: writes ENV_FILE with random local secrets (Postgres, Neo4j, session key, RustFS keys, the bundled
# Keycloak's admin, QA, web-client and personal-user passwords) and a fixed id for the personal user. It asks
# for your email (LOCAL_TEST_USER_EMAIL; Enter = git user.email; without a terminal dev@example.com), derives
# AUTH_ALLOWED_EMAIL_DOMAINS from it (your domain and example.com, written only when missing) and asks for the
# LLM and graph settings it cannot generate. Keycloak imports these values as your sign-in at container creation.
# Safe to run again after every update: it never overwrites a value and never prints one. A secret that is
# missing or empty is generated and appended, so a release that adds a secret needs only this command. A setting
# that is missing or empty is asked for when stdin is a terminal (Enter skips it; the API key is read without
# echo); without a terminal (CI, agents) nothing is asked and the missing names are listed. A value outside
# [A-Za-z0-9._:/@+=-] is written single-quoted, so compose takes it literally. A secret appended to a file whose
# volumes already hold data does not match them: `make down-volumes`.
#
# check-keycloak: fails, naming the names only (never a value), when ENV_FILE lacks a name the Keycloak realm
# is imported from or holds an unusable LOCAL_TEST_USER_EMAIL (compose defaults them to empty so that other
# commands still work).

# Generated secrets.
ENV_LOCAL_NAMES="POSTGRES_ADMIN_PASSWORD DATABASE_PASSWORD DATABASE_AGENT_PASSWORD NEO4J_PASSWORD \
SESSION_SECRET RUSTFS_ACCESS_KEY RUSTFS_SECRET_KEY \
KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_QA_PASSWORD KEYCLOAK_WEB_CLIENT_SECRET \
LOCAL_TEST_USER_PASSWORD LOCAL_TEST_USER_ID"
# The names `make up` needs in the env file (the Keycloak realm is imported from them).
KEYCLOAK_ENV_NAMES="KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_QA_PASSWORD KEYCLOAK_WEB_CLIENT_SECRET \
LOCAL_TEST_USER_EMAIL LOCAL_TEST_USER_PASSWORD LOCAL_TEST_USER_ID"
# LLM settings asked for (never generated).
ENV_LOCAL_INPUT_NAMES="AGENT_MODEL LLM_GATEWAY_URL LLM_GATEWAY_API_KEY"

# The last value of $1 in the env file, without quotes, spaces or CR.
val() { sed -n "s/^$1=//p" "$f" 2>/dev/null | tail -n 1 | tr -d "\"' \r"; }

# Is $1 usable as the personal sign-in email: one @, a dotted domain, only [A-Za-z0-9._:/@+=-], and not one of
# the QA users of the realm.
email_ok() {
	case "$1" in *@*) ;; *) return 1;; esac
	case "$1" in *[!A-Za-z0-9._:/@+=-]*|@*|*@|*@*@*) return 1;; esac
	case "${1#*@}" in .*|*.|*..*) return 1;; *.*) ;; *) return 1;; esac
	case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in alice@example.com|bob@example.com|carol@other.example) return 1;; esac
}

gen() {
	case $1 in
		RUSTFS_ACCESS_KEY) openssl rand -hex 10;;
		LOCAL_TEST_USER_ID) openssl rand -hex 16 | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/';;
		*) openssl rand -hex 32;;
	esac
}

# Replace (or add) $1=$2 in the env file atomically, mode 600.
put() {
	t="$f.tmp.$$"; { grep -v "^$1=" "$f" 2>/dev/null || :; } > "$t"
	printf '%s=%s\n' "$1" "$2" >> "$t"; chmod 600 "$t"; mv "$t" "$f"
}

# put, single-quoting a value outside [A-Za-z0-9._:/@+=-]; refuses a value holding a single quote.
store() {
	case "$2" in
		*"'"*) echo "env-local: $1 holds a single quote; set it in $f by hand" >&2; return 1;;
		*[!A-Za-z0-9._:/@+=-]*) put "$1" "'$2'";;
		*) put "$1" "$2";;
	esac
	set="$set $1"
}

write_secrets() {
	if [ ! -e "$f" ]; then
		tmp="$f.tmp.$$"; : > "$tmp"
		for name in $ENV_LOCAL_NAMES; do
			printf '%s=%s\n' "$name" "$(gen "$name")" >> "$tmp"
		done
		# shellcheck disable=SC2086 # the names, one space apart
		if ln "$tmp" "$f" 2>/dev/null; then echo "env-local: wrote $f ($(echo $ENV_LOCAL_NAMES))"; fi
		rm -f "$tmp"
	fi
	for name in $ENV_LOCAL_NAMES; do
		[ -n "$(val "$name")" ] && continue
		put "$name" "$(gen "$name")"; added="$added $name"
	done
}

write_identity() {
	if [ -z "$(val LOCAL_TEST_USER_EMAIL)" ]; then
		email=""; gitmail="$(git config user.email 2>/dev/null | tr 'A-Z' 'a-z' | tr -d ' \r')"
		if [ -t 0 ]; then
			tries=0
			while [ -z "$email" ] && [ $tries -lt 3 ]; do
				printf 'env-local: LOCAL_TEST_USER_EMAIL (Enter = %s): ' "${gitmail:-no default}" >&2
				IFS= read -r answer || answer=""
				answer="$(printf '%s' "${answer:-$gitmail}" | tr 'A-Z' 'a-z' | tr -d ' \r')"
				if email_ok "$answer"; then email="$answer"
				else echo "env-local: not usable as your sign-in email (one @, a dotted domain, characters A-Za-z0-9._:/@+=-, not a QA user)" >&2; fi
				tries=$((tries + 1))
			done
		fi
		if [ -z "$email" ]; then email=dev@example.com; [ ! -t 0 ] || echo "env-local: no usable email given, using $email" >&2; fi
		put LOCAL_TEST_USER_EMAIL "$email"; added="$added LOCAL_TEST_USER_EMAIL"
	fi
	domain="$(val LOCAL_TEST_USER_EMAIL | tr 'A-Z' 'a-z')"
	if email_ok "$domain"; then domain="${domain#*@}"
	else domain=""; echo "env-local: LOCAL_TEST_USER_EMAIL in $f is not usable as a sign-in email (one @, a dotted domain, characters A-Za-z0-9._:/@+=-, not a QA user); fix it by hand" >&2; fi
	domains="$(val AUTH_ALLOWED_EMAIL_DOMAINS)"
	if [ -z "$domains" ] && [ -n "$domain" ]; then
		if [ "$domain" = example.com ]; then want=example.com; else want="$domain,example.com"; fi
		put AUTH_ALLOWED_EMAIL_DOMAINS "$want"; added="$added AUTH_ALLOWED_EMAIL_DOMAINS"
	elif [ -n "$domain" ]; then
		lacks=""
		for d in $(printf '%s\n' "$domain" example.com | sort -u); do
			case ",$(printf '%s' "$domains" | tr 'A-Z' 'a-z')," in *",$d,"*) ;; *) lacks="$lacks $d";; esac
		done
		[ -z "$lacks" ] || echo "env-local: AUTH_ALLOWED_EMAIL_DOMAINS in $f lacks$lacks; sign-in of those users is refused (domain_not_allowed)" >&2
	fi
}

ask_llm() {
	for name in $ENV_LOCAL_INPUT_NAMES; do
		[ -n "$(val "$name")" ] && continue
		answer=""
		if [ -t 0 ]; then
			case $name in
				AGENT_MODEL) hint="the model name your LLM gateway serves";;
				LLM_GATEWAY_URL) hint="the URL of your OpenAI-compatible gateway";;
				*) hint="input hidden";;
			esac
			printf 'env-local: %s (%s; Enter skips): ' "$name" "$hint" >&2
			if [ "$name" = LLM_GATEWAY_API_KEY ]; then
				trap 'stty echo 2>/dev/null; exit 130' INT TERM; stty -echo 2>/dev/null
				IFS= read -r answer || answer=""
				stty echo 2>/dev/null; trap - INT TERM; echo >&2
			else IFS= read -r answer || answer=""; fi
		fi
		if [ -z "$answer" ]; then missing="$missing $name"; continue; fi
		store "$name" "$answer" || missing="$missing $name"
	done
}

ask_graph_models() {
	for name in GRAPH_LLM_MODEL GRAPH_SMALL_MODEL; do
		[ -n "$(val "$name")" ] && continue
		default="$(val AGENT_MODEL)"; answer=""
		if [ -t 0 ]; then
			case $name in GRAPH_LLM_MODEL) hint="the graph build's main model";; *) hint="the graph build's small model";; esac
			printf 'env-local: %s (%s; Enter = %s): ' "$name" "$hint" "${default:-skip}" >&2
			IFS= read -r answer || answer=""; answer="${answer:-$default}"
		fi
		if [ -z "$answer" ]; then missing="$missing $name"; continue; fi
		store "$name" "$answer" || missing="$missing $name"
	done
}

# GRAPH_EMBEDDING_MODEL is written only together with GRAPH_EMBEDDING_DIM (no default: the model's vector size).
ask_embedding() {
	model="$(val GRAPH_EMBEDDING_MODEL)"; dim="$(val GRAPH_EMBEDDING_DIM)"; typed=""
	if [ -z "$model" ]; then
		answer=""
		if [ -t 0 ]; then
			printf 'env-local: GRAPH_EMBEDDING_MODEL (the embedding model your LLM gateway serves; Enter skips): ' >&2
			IFS= read -r answer || answer=""
		fi
		case "$answer" in
			"") ;;
			*"'"*) echo "env-local: GRAPH_EMBEDDING_MODEL holds a single quote; set it in $f by hand" >&2;;
			*) typed="$answer";;
		esac
		if [ -z "$typed" ]; then missing="$missing GRAPH_EMBEDDING_MODEL"; [ -n "$dim" ] || missing="$missing GRAPH_EMBEDDING_DIM"
		elif [ -n "$dim" ]; then echo "env-local: GRAPH_EMBEDDING_DIM in $f is kept; it must be the vector size of $typed" >&2; fi
	fi
	if [ -z "$dim" ] && [ -n "$model$typed" ]; then
		tries=0
		if [ -t 0 ]; then
			while [ -z "$dim" ] && [ $tries -lt 3 ]; do
				printf 'env-local: GRAPH_EMBEDDING_DIM (required, no default: the vector size of %s, a whole number from 1 to 4096): ' "${typed:-$model}" >&2
				IFS= read -r answer || answer=""; answer="$(printf '%s' "$answer" | tr -d ' \r')"
				case "$answer" in
					""|0*|*[!0-9]*|?????*) ;;
					*) [ "$answer" -le 4096 ] && dim="$answer";;
				esac
				[ -n "$dim" ] || echo "env-local: GRAPH_EMBEDDING_DIM must be a whole number from 1 to 4096: the vector size of the embedding model" >&2
				tries=$((tries + 1))
			done
		fi
		if [ -n "$dim" ]; then put GRAPH_EMBEDDING_DIM "$dim"; set="$set GRAPH_EMBEDDING_DIM"
		else
			missing="$missing GRAPH_EMBEDDING_DIM"
			if [ -n "$typed" ]; then echo "env-local: GRAPH_EMBEDDING_MODEL not written: it needs GRAPH_EMBEDDING_DIM" >&2; missing="$missing GRAPH_EMBEDDING_MODEL"; fi
		fi
	fi
	if [ -n "$typed" ] && [ -n "$dim" ]; then store GRAPH_EMBEDDING_MODEL "$typed" || missing="$missing GRAPH_EMBEDDING_MODEL"; fi
}

write() {
	command -v openssl >/dev/null || { echo "env-local: openssl not found" >&2; exit 1; }
	umask 077
	added=""; set=""; missing=""
	write_secrets
	write_identity
	[ -z "$added" ] || echo "env-local: appended$added to $f"
	ask_llm
	ask_graph_models
	ask_embedding
	[ -z "$set" ] || echo "env-local: set$set in $f"
	[ -z "$missing" ] || echo "env-local: add$missing to $f (docs/configuration.md)"
	[ -n "$added$set$missing" ] || echo "env-local: $f is complete, left unchanged"
}

check_keycloak() {
	missing=""
	for name in $KEYCLOAK_ENV_NAMES; do
		[ -n "$(val "$name")" ] || missing="$missing $name"
	done
	[ -z "$missing" ] || { echo "up: $f lacks$missing; run make env-local" >&2; exit 1; }
	email="$(val LOCAL_TEST_USER_EMAIL)"
	email_ok "$email" || { echo "up: LOCAL_TEST_USER_EMAIL in $f is not usable as a sign-in email; fix it, or delete the line and run make env-local" >&2; exit 1; }
}

usage() { echo "usage: sh scripts/env-local.sh write|check-keycloak ENV_FILE" >&2; exit 2; }

[ $# -eq 2 ] && [ -n "$2" ] || usage
f="$2"
case "$1" in
	write) write;;
	check-keycloak) check_keycloak;;
	*) usage;;
esac
