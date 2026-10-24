# Vendored browser libraries

Spot serves every browser dependency itself so the UI works on an internal
network with no internet access. Nothing is loaded from a CDN.

| File | Package | Version | Source tarball SHA-1 (npm `dist.shasum`) |
|------|---------|---------|------------------------------------------|
| `chart-4.4.4.umd.min.js` | [chart.js](https://www.npmjs.com/package/chart.js) (`dist/chart.umd.js`) | 4.4.4 | `b682d2e7249f7a0cbb1b1d31c840266ae9db64b7` |
| `chartjs-adapter-date-fns-3.0.0.bundle.min.js` | [chartjs-adapter-date-fns](https://www.npmjs.com/package/chartjs-adapter-date-fns) (`dist/chartjs-adapter-date-fns.bundle.min.js`, includes date-fns) | 3.0.0 | `c25f63c7f317c1f96f9a7c44bd45eeedb8a478e5` |

Both are MIT licensed; licence texts are alongside.

To upgrade: download the package tarball from the npm registry
(`https://registry.npmjs.org/<pkg>/-/<pkg>-<ver>.tgz`), check its SHA-1
against `dist.shasum` from `https://registry.npmjs.org/<pkg>/<ver>`, copy the
file above out of `package/dist/`, rename it with the new version, update
`app/templates/base.html`, and update this table.
