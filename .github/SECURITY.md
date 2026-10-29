# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately
to the security team at security@griddynamics.com. Do not create
a public issue.

You can also use GitHub private vulnerability reporting: open the
**Security** tab of this repository and choose **Report a vulnerability**.

## Best Practices

- Do not hard-code credentials or secrets.
- Use environment variables and GitHub Secrets for CI/CD.
- Follow secure coding guidelines and conduct code reviews.

## CI/CD Secrets

All credentials should be stored securely using GitHub Actions Secrets or a secrets manager. Never commit them to the repository.
