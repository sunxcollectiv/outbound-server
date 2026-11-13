#!/usr/bin/env bash
# Writes the .env for one environment, so every machine's file has the same
# layout and differs only in values:
#
#   deploy/newenv.sh <local|staging|prod> [--force]
#
# Run it in the checkout that will use it: your laptop for local, the server
# for staging or prod. It generates the Postgres password, sets the
# per-environment switches, and asks for each secret in turn (Enter leaves one
# blank and its feature off). A value already exported in the shell is used
# without asking, so it also runs unattended:
#
#   GOOGLE_MAPS_API_KEY=... deploy/newenv.sh prod </dev/null
#
# It refuses to replace an existing .env without --force: a new
# POSTGRES_PASSWORD would not match the existing pgdata volume, and the symptom
# (auth failures against your own database) looks nothing like the cause. With
# --force the old file is copied to .env.bak.<timestamp> first and its values,
# the password included, are kept as the defaults -- so re-running with --force
# and pressing Enter through the prompts is how to change a single key.

set -euo pipefail
cd "$(dirname "$0")/.."

usage() { echo "usage: $0 <local|staging|prod> [--force]" >&2; exit 2; }
ENV_NAME="${1:-}"
FORCE="${2:-}"
case "$ENV_NAME" in local|staging|prod) ;; *) usage ;; esac
[[ -z "$FORCE" || "$FORCE" == "--force" ]] || usage

if [[ -e .env && "$FORCE" != "--force" ]]; then
    echo ".env already exists. Pass --force to rewrite it; it is backed up first and" >&2
    echo "its values, POSTGRES_PASSWORD included, are kept unless you change them." >&2
    exit 1
fi

# old KEY: the key's value in the current .env. (No associative arrays: macOS
# ships bash 3.2.)
old() { [[ -e .env ]] && sed -n "s/^$1=//p" .env | tail -n 1 || true; }

INTERACTIVE=0
[[ -t 0 ]] && INTERACTIVE=1

# ask VAR "prompt" [default] [secret]
# VAR comes from the shell environment if set there; otherwise from a prompt
# whose default is the old .env value, else the given default.
ask() {
    local var="$1" prompt="$2" secret="${4:-}" default value=""
    default="$(old "$1")"; default="${default:-${3:-}}"
    if [[ -n "${!var:-}" ]]; then
        value="${!var}"
    elif (( INTERACTIVE )); then
        local shown="$default"
        [[ -n "$secret" && -n "$default" ]] && shown="keep current"
        if [[ -n "$secret" ]]; then
            read -r -s -p "$prompt${shown:+ [$shown]}: " value; echo
        else
            read -r -p "$prompt${shown:+ [$shown]}: " value
        fi
        value="${value:-$default}"
    else
        value="$default"
    fi
    # Compose interpolates $ in .env, so a value containing one would be mangled.
    if [[ "$value" == *'$'* ]]; then
        echo "$var contains '\$'. Leave it blank here and add it to .env by hand as '\$\$'." >&2
        exit 1
    fi
    printf -v "$var" '%s' "$value"
}

SERVER=0
[[ "$ENV_NAME" != local ]] && SERVER=1

# --- Postgres -------------------------------------------------------------------
POSTGRES_USER=wravel
POSTGRES_DB=wravel
POSTGRES_PORT=4930
# hex: it is interpolated into a DSN, and hex needs no percent-encoding there.
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(old POSTGRES_PASSWORD)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')}"

# --- Per-environment switches ------------------------------------------------------
case "$ENV_NAME" in
    # Xcode builds get sandbox push tokens; TestFlight and App Store ones do not.
    local)   APNS_USE_SANDBOX_DEFAULT=true;  STOREKIT_ALLOW_XCODE=true ;;
    staging) APNS_USE_SANDBOX_DEFAULT=true;  STOREKIT_ALLOW_XCODE=false ;;
    prod)    APNS_USE_SANDBOX_DEFAULT=false; STOREKIT_ALLOW_XCODE=false ;;
esac

# --- Secrets --------------------------------------------------------------------------
echo "Writing .env for $ENV_NAME. Enter leaves a key blank and its feature off."
ask GOOGLE_MAPS_API_KEY    "Google Maps API key" "" secret
ask UNSPLASH_ACCESS_KEY    "Unsplash access key" "" secret
ask ANTHROPIC_API_KEY      "Anthropic API key" "" secret
ask ANTHROPIC_WORKSPACE_ID "Anthropic workspace id (org-level keys only)"
ask RESEND_API_KEY         "Resend API key" "" secret
ask RESEND_WEBHOOK_SECRET  "Resend webhook signing secret (whsec_...)" "" secret

ask APNS_KEY_ID  "APNs key id"
ask APNS_TEAM_ID "Apple team id"
APNS_PRIVATE_KEY="${APNS_PRIVATE_KEY:-$(old APNS_PRIVATE_KEY)}"
if [[ -n "$APNS_KEY_ID" ]]; then
    # Inlined from the .p8 file, newlines as \n (app/services/apns.py expands
    # them), so no key file has to be mounted into the container.
    APNS_KEY_FILE="${APNS_KEY_FILE:-}"
    if [[ -z "$APNS_KEY_FILE" ]] && (( INTERACTIVE )); then
        read -r -p "Path to the APNs .p8 key file${APNS_PRIVATE_KEY:+ [keep current]}: " APNS_KEY_FILE
    fi
    if [[ -n "$APNS_KEY_FILE" ]]; then
        [[ -f "$APNS_KEY_FILE" ]] || { echo "No file at $APNS_KEY_FILE." >&2; exit 1; }
        APNS_PRIVATE_KEY="$(awk 'NF { printf "%s\\n", $0 }' "$APNS_KEY_FILE")"
    fi
    [[ -n "$APNS_PRIVATE_KEY" ]] || { echo "APNS_KEY_ID is set but there is no key file." >&2; exit 1; }
else
    APNS_PRIVATE_KEY=""
fi
ask APNS_USE_SANDBOX "APNs sandbox (true for Xcode builds, false for TestFlight/App Store)" "$APNS_USE_SANDBOX_DEFAULT"

# Only prod takes App Store purchases.
if [[ "$ENV_NAME" == prod ]]; then
    ask STOREKIT_APP_APPLE_ID "App Store numeric Apple ID (production purchases are refused without it)"
else
    STOREKIT_APP_APPLE_ID=""
fi

# --- Write ------------------------------------------------------------------------------
if [[ -e .env ]]; then
    backup=".env.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$backup"
    echo "backed up the old .env to $backup"
fi

# A blank value is written commented out, so the app sees the key as unset
# (a bare "KEY=" is an empty string, which an int setting rejects).
line() { if [[ -n "$2" ]]; then echo "$1=$2"; else echo "# $1="; fi; }

# 600 from the first byte. It holds the database password and every secret.
umask 077
{
    echo "# Generated by deploy/newenv.sh $ENV_NAME. Never commit this file."
    echo "# Re-run it with --force rather than editing by hand, so every"
    echo "# environment keeps the same layout."
    echo "ENVIRONMENT=$ENV_NAME"
    echo
    if (( SERVER )); then
        echo "# Switches on the app service in docker-compose.yml. Absent locally,"
        echo "# where the API runs from the venv."
        echo "COMPOSE_PROFILES=server"
        echo
    fi
    echo "# --- Postgres -----------------------------------------------------------"
    echo "# Read by docker-compose.yml. Changing the password here does not change it"
    echo "# in an existing database."
    echo "POSTGRES_USER=$POSTGRES_USER"
    echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
    echo "POSTGRES_DB=$POSTGRES_DB"
    echo "# Host-side publish, loopback only. 4930 on every machine."
    echo "POSTGRES_PORT=$POSTGRES_PORT"
    if (( SERVER )); then
        echo "# No DATABASE_URL: docker-compose.yml builds the app's from the values above."
        echo
        echo "# --- Server -------------------------------------------------------------"
        echo "# Host-side port the API is published on, loopback only. nginx proxies here."
        echo "PORT=4830"
    else
        echo "# For the API and scripts running from the venv."
        echo "DATABASE_URL=postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:$POSTGRES_PORT/$POSTGRES_DB"
    fi
    echo
    echo "# --- Integrations -------------------------------------------------------"
    echo "# Google Maps: place search and Place Details"
    line GOOGLE_MAPS_API_KEY "$GOOGLE_MAPS_API_KEY"
    echo "# Unsplash: area cover photos"
    line UNSPLASH_ACCESS_KEY "$UNSPLASH_ACCESS_KEY"
    echo "# Anthropic: parsing forwarded emails and screenshots"
    line ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
    line ANTHROPIC_WORKSPACE_ID "$ANTHROPIC_WORKSPACE_ID"
    echo "# Resend: forwarded travel-confirmation emails and their webhook"
    line RESEND_API_KEY "$RESEND_API_KEY"
    line RESEND_WEBHOOK_SECRET "$RESEND_WEBHOOK_SECRET"
    echo
    echo "# --- Apple push notifications --------------------------------------------"
    line APNS_KEY_ID "$APNS_KEY_ID"
    line APNS_TEAM_ID "$APNS_TEAM_ID"
    line APNS_PRIVATE_KEY "$APNS_PRIVATE_KEY"
    line APNS_USE_SANDBOX "$APNS_USE_SANDBOX"
    echo
    echo "# --- StoreKit in-app purchases -------------------------------------------"
    line STOREKIT_APP_APPLE_ID "$STOREKIT_APP_APPLE_ID"
    echo "# Unsigned Xcode purchases: local only, since anyone could forge one."
    line STOREKIT_ALLOW_XCODE "$STOREKIT_ALLOW_XCODE"
} > .env

echo "wrote $PWD/.env (mode 600)"
echo
if (( SERVER )); then
    echo "Next:  deploy/deploy.sh"
else
    echo "Next:  docker compose up -d db && uv run python scripts/bootstrap.py"
fi
