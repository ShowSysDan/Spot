# Vendored browser libraries

Spot serves every browser dependency itself so the UI works on an internal
network with no internet access. Nothing is loaded from a CDN.

| File | Package (file in tarball) | Version | npm tarball SHA-1 (`dist.shasum`) |
|------|---------------------------|---------|-----------------------------------|
| `chart-4.4.4.umd.min.js` | [chart.js](https://www.npmjs.com/package/chart.js) (`dist/chart.umd.js`) | 4.4.4 | `b682d2e7249f7a0cbb1b1d31c840266ae9db64b7` |
| `luxon-3.5.0.min.js` | [luxon](https://www.npmjs.com/package/luxon) (`build/global/luxon.min.js`) | 3.5.0 | `6b6f65c5cd1d61d1fd19dbf07ee87a50bf4b8e20` |
| `chartjs-adapter-luxon-1.3.1.umd.min.js` | [chartjs-adapter-luxon](https://www.npmjs.com/package/chartjs-adapter-luxon) (`dist/chartjs-adapter-luxon.umd.min.js`) | 1.3.1 | `8ad8be7cc1521bacd6cd2ff4b013669d4dd49364` |

The only change from the published files: the trailing `//# sourceMappingURL=`
comment is removed, since the `.map` files aren't shipped (it would just make
browser dev tools log a 404).

Luxon (not date-fns) is the Chart.js date adapter because it can render time
axes in a fixed timezone — Spot shows every time in `SPOT_TIMEZONE`
(default America/New_York, 24-hour) regardless of the viewer's computer.

All three are MIT licensed; licence texts are alongside.

To upgrade: download the tarball from the npm registry
(`https://registry.npmjs.org/<pkg>/-/<pkg>-<ver>.tgz`), check its SHA-1
against `dist.shasum` from `https://registry.npmjs.org/<pkg>/<ver>`, copy the
file listed above out of the tarball, drop the sourceMappingURL line, rename
it with the new version, update `app/templates/base.html`, and update this
table.
