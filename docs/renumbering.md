# SirisAI renumbering (2026-11-22)

Brad's rule: a whole-number change is a significant new section or area (like Health), +0.1 a new feature or a bigger
upgrade, +0.01 a fix or minor change. Decimals rolled over (1.9 + 0.1 = 2.0), so versions are now written
**Section.Feature.Fix** and each part counts on its own. Every release was judged a section, a feature or a fix and the
history counted again from 1.0.0, in the order the releases shipped. GitHub's tags and releases were recreated under the
new numbers on the same commits. (6.68 was a tag with no changelog section; it's 4.14.1 now.)

The new sections: servers over SSH (2.0.0), Health (3.0.0) and Life admin (4.0.0).

| Was | Now | Kind |
|---|---|---|
| 1.0 | 1.0.0 | first |
| 1.1 | 1.1.0 | feature |
| 1.2 | 1.2.0 | feature |
| 1.21 | 1.2.1 | fix |
| 1.22 | 1.2.2 | fix |
| 1.32 | 1.3.0 | feature |
| 1.42 | 1.4.0 | feature |
| 1.52 | 1.5.0 | feature |
| 1.62 | 1.6.0 | feature |
| 1.63 | 1.6.1 | fix |
| 1.73 | 1.7.0 | feature |
| 1.74 | 1.7.1 | fix |
| 1.84 | 1.8.0 | feature |
| 1.94 | 2.0.0 | section |
| 2.04 | 2.1.0 | feature |
| 2.14 | 2.2.0 | feature |
| 2.24 | 2.3.0 | feature |
| 2.25 | 2.3.1 | fix |
| 2.26 | 2.3.2 | fix |
| 2.36 | 2.4.0 | feature |
| 2.46 | 2.5.0 | feature |
| 2.56 | 2.6.0 | feature |
| 2.66 | 2.6.1 | fix |
| 2.76 | 2.7.0 | feature |
| 2.77 | 2.7.1 | fix |
| 2.87 | 2.8.0 | feature |
| 2.88 | 2.8.1 | fix |
| 2.89 | 2.8.2 | fix |
| 2.9 | 2.8.3 | fix |
| 2.91 | 2.9.0 | feature |
| 3.01 | 2.9.1 | fix |
| 3.11 | 2.10.0 | feature |
| 3.21 | 2.11.0 | feature |
| 3.31 | 2.12.0 | feature |
| 3.41 | 2.13.0 | feature |
| 3.51 | 3.0.0 | section |
| 3.52 | 3.0.1 | fix |
| 3.62 | 3.1.0 | feature |
| 3.72 | 3.2.0 | feature |
| 3.82 | 3.3.0 | feature |
| 3.83 | 3.3.1 | fix |
| 3.93 | 3.3.2 | fix |
| 4.03 | 3.4.0 | feature |
| 4.04 | 3.4.1 | fix |
| 4.05 | 3.4.2 | fix |
| 4.06 | 3.4.3 | fix |
| 4.16 | 3.5.0 | feature |
| 4.17 | 3.6.0 | feature |
| 4.18 | 3.6.1 | fix |
| 4.28 | 3.7.0 | feature |
| 4.29 | 3.8.0 | feature |
| 4.39 | 3.9.0 | feature |
| 4.49 | 3.10.0 | feature |
| 4.5 | 3.10.1 | fix |
| 4.6 | 4.0.0 | section |
| 4.7 | 4.1.0 | feature |
| 4.8 | 4.1.1 | fix |
| 4.9 | 4.2.0 | feature |
| 5.0 | 4.3.0 | feature |
| 5.01 | 4.3.1 | fix |
| 5.11 | 4.3.2 | fix |
| 5.21 | 4.4.0 | feature |
| 5.31 | 4.5.0 | feature |
| 5.41 | 4.6.0 | feature |
| 5.51 | 4.6.1 | fix |
| 5.52 | 4.6.2 | fix |
| 5.53 | 4.6.3 | fix |
| 5.63 | 4.6.4 | fix |
| 5.73 | 4.7.0 | feature |
| 5.83 | 4.8.0 | feature |
| 5.93 | 4.9.0 | feature |
| 5.94 | 4.9.1 | fix |
| 6.04 | 4.10.0 | feature |
| 6.05 | 4.10.1 | fix |
| 6.06 | 4.10.2 | fix |
| 6.07 | 4.10.3 | fix |
| 6.08 | 4.10.4 | fix |
| 6.18 | 4.10.5 | fix |
| 6.28 | 4.11.0 | feature |
| 6.38 | 4.12.0 | feature |
| 6.48 | 4.13.0 | feature |
| 6.58 | 4.14.0 | feature |
| 6.68 | 4.14.1 | fix |
| 6.59 | 4.14.2 | fix |
| 6.6 | 4.14.3 | fix |
| 6.7 | 4.15.0 | feature |
| 6.71 | 4.15.1 | fix |
| 6.72 | 4.15.2 | fix |
| 6.73 | 4.15.3 | fix |
| 6.74 | 4.15.4 | fix |
| 6.75 | 4.15.5 | fix |
| 6.76 | 4.15.6 | fix |
| 6.77 | 4.15.7 | fix |
| 6.87 | 4.16.0 | feature |
| 6.97 | 4.17.0 | feature |
| 7.07 | 4.18.0 | feature |
| 7.17 | 4.19.0 | feature |
| 7.27 | 4.20.0 | feature |
| 7.28 | 4.21.0 | feature |
| 7.29 | 4.21.1 | fix |
| 7.39 | 4.22.0 | feature |
| 7.49 | 4.23.0 | feature |
| 7.5 | 4.23.1 | fix |
| 7.6 | 4.24.0 | feature |
| 7.61 | 4.24.1 | fix |
