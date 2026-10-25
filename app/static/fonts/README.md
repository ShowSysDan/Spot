# Poppins (Spot's brand font)

Served from here, not a CDN, so the UI and PDF reports work on an internal
network with no internet access. Used by the web UI (`style.css`
`@font-face`), PDF reports (`app/exports.py` registers the TTFs with
ReportLab) and their charts (matplotlib).

| Files | Package (path in tarball) | Version | npm tarball SHA-1 (`dist.shasum`) |
|-------|---------------------------|---------|-----------------------------------|
| `Poppins-{Regular,Italic,SemiBold,Bold}.ttf` | [@expo-google-fonts/poppins](https://www.npmjs.com/package/@expo-google-fonts/poppins) (`400Regular/Poppins_400Regular.ttf`, `400Regular_Italic/…`, `600SemiBold/…`, `700Bold/…`, renamed) | 0.4.1 | `bf49b2a14a5d255fa3404fdef7bcbbb04025f0a4` |
| `poppins-latin-*.woff2` | [@fontsource/poppins](https://www.npmjs.com/package/@fontsource/poppins) (`files/poppins-latin-*.woff2`, unchanged) | 5.3.0 | `2b0b95ad6d221ca81f884796429b209837954b89` |

Both are the Google Fonts release of Poppins by Indian Type Foundry,
licensed under the SIL Open Font License 1.1 (`OFL.txt`).

Poppins has no ■ or → glyphs: the PDF draws its colour swatches with
ReportLab's built-in ZapfDingbats, and browsers fall back to a system font
for the arrows the web UI uses.
