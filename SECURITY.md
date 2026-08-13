# Security policy

## Supported versions

Security fixes are provided for the latest released version. Operators should
also keep Docker, the host operating system, and Calibre-Web-Automated current.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** form in this repository's Security tab.
Do not disclose a suspected vulnerability in a public issue, discussion, or
pull request.

Include the affected version, deployment architecture, impact, reproduction
steps using synthetic data, and any suggested mitigation. Remove credentials,
client addresses, book metadata, OPDS URLs, cache keys, certificate private
keys, and administration state before submitting evidence.

You should receive an acknowledgement within seven days. A fix, coordinated
disclosure plan, or status update will follow after validation. Please allow a
reasonable remediation period before public disclosure.

## Security boundaries

The bridge processes untrusted OPDS XML and EPUB ZIP content. Its controls
include fixed-origin proxying, entity/network-disabled XML parsers, archive
path validation, expansion and entry limits, image decoder and pixel limits,
atomic cache publication, a read-only capability-dropped container, and an
administration listener isolated behind HTTPS authentication.

The bridge does not replace secure CWA passwords, host patching, network
segmentation, trusted backups, or TLS certificate verification. A self-signed
certificate must be verified using the fingerprint printed locally at setup.
