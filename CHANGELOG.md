# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-09-04

### Added
- Third bar with the per-model weekly limit (currently **Fable**), read from
  the `limits` array of the usage response. Shown only when the API reports
  one; otherwise the two-bar layout is used.
- Compact three-bar layout. The weekly limits reset together, so the
  "Resets in" line is drawn once, under the last weekly bar.
- Test suite (`pytest`, see `requirements-dev.txt`) with a real API response
  as a fixture.
- `--version` flag.

### Fixed
- **Cache poisoning.** An HTTP 200 with an empty or partial body no longer
  overwrites cached values. Every block carries its own timestamp and
  expires independently.
- A failure in our own response parsing is no longer disguised as an API
  outage: the script renders the last cached values, prints the traceback
  and exits with code 2.
- README images: `docs/preview.png` was hidden by a `.gitignore` rule and
  never committed.

### Changed
- Percent numbers are drawn in the heaviest available Inter weight, with a
  fixed gap before the `%` sign regardless of font kerning.
- Pill labels are centred by cap height, so "Weekly" (with a descender)
  matches "Current"; the header title and clock are aligned with the mascot.
- Bottom safe margin: the SmallTV panel clips the last rows of the image.

## [1.0.1] — 2026-07-20

### Fixed
- Cloudflare challenge on server IPs: `curl_cffi` now impersonates Safari
  instead of Chrome.

## [1.0.0] — 2026-05-21

- Initial release: 5-hour and weekly bars, 5-minute cache with a 12-hour
  fallback, Cloudflare bypass via `curl_cffi` TLS impersonation.

[1.1.0]: https://github.com/egerkuzma/geekclock-claude/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/egerkuzma/geekclock-claude/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/egerkuzma/geekclock-claude/releases/tag/v1.0.0
