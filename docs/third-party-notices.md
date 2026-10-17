---
title: Third-party notices
icon: lucide/scale
description: Licenses for the third-party code distributed by the bf documentation site.
hide:
  - toc
---

# Third-party notices

The documentation site distributes these pinned third-party artifacts. Python distributions include a separate checked-in [`THIRD_PARTY_NOTICES.md`](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md) covering Brain Framework's runtime dependency closure.

| Component               | Version | Distributed use                           | License                                                           |
| ----------------------- | ------- | ----------------------------------------- | ----------------------------------------------------------------- |
| Zensical                | 0.0.71  | Templates, styles, and site JavaScript    | [MIT](third-party/zensical-LICENSE.txt)                           |
| Lucide Icons            | bundled | Inline interface icons                    | [ISC and MIT](third-party/lucide-LICENSE.txt)                     |
| Simple Icons            | bundled | GitHub and PyPI brand icons               | [CC0-1.0](third-party/simple-icons-LICENSE.txt)                   |
| Zensical browser bundle | 0.0.71  | Clipboard, escaping, focus, and RxJS code | [Component licenses](third-party/zensical-javascript-LICENSE.txt) |
| Preact                  | bundled | Search dialog rendering                   | [MIT](third-party/preact-LICENSE.txt)                             |
| Preact Signals          | bundled | Search dialog state                       | [MIT](third-party/preact-signals-LICENSE.txt)                     |
| clsx                    | bundled | Search dialog class names                 | [MIT](third-party/clsx-LICENSE.txt)                               |
| usehooks-ts             | bundled | Search dialog hooks                       | [MIT](third-party/usehooks-ts-LICENSE.txt)                        |
| lodash.debounce         | bundled | Debounce helper that usehooks-ts requires | [MIT](third-party/lodash-debounce-LICENSE.txt)                    |

The browser bundle's search dialog contains Preact, Preact Signals (`@preact/signals` and `@preact/signals-core`), clsx, usehooks-ts and lodash.debounce, which the bundle's license file omits and whose versions Zensical does not record. Their license texts are exact copies from each package's latest npm release and match the earlier releases checked. The bundle's clipboard.js build also embeds tiny-emitter, good-listener, select and delegate, which neither its license file nor this page lists separately; good-listener, select and delegate share clipboard.js's MIT license and author. The other linked license texts are exact copies from the locked Zensical distribution. All of them are deployed with every site build.
