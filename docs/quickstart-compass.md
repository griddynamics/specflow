# Compass/P10Y Setup

SpecFlow uses [Compass by P10Y](https://compass.p10y.com) to measure generated
code complexity and compare implementation variants.

## Create a Compass Account

Go to [compass.p10y.com](https://compass.p10y.com) and create an account.
Compass API access requires an enterprise account.

## Connect GitHub

1. Open Compass settings.
2. Go to Integrations -> New Integration.
3. Choose GitHub.
4. Add the GitHub username or organization that owns your SpecFlow workspace repos.
5. Paste a GitHub PAT with access: `admin:repo_hook, repo:*, user:*, workflow:*`.
6. Enable auto-discovery for repositories.
7. Save the integration.


## Create the API Token

1. Open Settings -> API Tokens.
2. Generate a token.
3. Put it in `.env` as `P10Y_API_KEY`.

`specflow-init.sh` calls Compass to resolve the organization id for this token
and writes `P10Y_ORGANISATION_ID` back to `.env`. You do not need to set that
value manually.
