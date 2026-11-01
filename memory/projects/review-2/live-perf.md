# Live performance capture (production build, local stack, Chromium)

Settle = load until network idle. LCP/CLS from PerformanceObserver. JS = decoded bytes of all scripts on the route. CLS target is < 0.1.

| Route | Settle ms (desk/phone) | LCP ms | CLS desk | CLS phone | JS KB | Largest chunk | Long tasks ms | DOM nodes | Horizontal overflow |
|---|---|---|---|---|---|---|---|---|---|
| about | 1560/1510 | 128 | 0.055 | 0.234 | 176 | index-BBII0rmU.js | 0 | 516 | — |
| account | 1526/1515 | 124 | 0.044 | 0.169 | 173 | index-BBII0rmU.js | 0 | 177 | — |
| admin | 1680/1625 | 128 | 1.281 | 1.169 | 246 | AdminPage-DyhGoz2z.js | 0 | 485 | — |
| agents | 1602/1674 | 124 | 0.507 | 0.998 | 355 | AgentsPage-C3AnC4qU.js | 0 | 188 | — |
| alerts | 1512/1534 | 68 | 0.009 | 0.113 | 199 | index-BBII0rmU.js | 0 | 74 | — |
| browse-misc | 1723/1880 | 180 | 0.136 | 0.301 | 172 | index-BBII0rmU.js | 0 | 155 | tablet 806>768 |
| browse-nested | 1597/1587 | 88 | 0.028 | 0.187 | 172 | index-BBII0rmU.js | 0 | 103 | — |
| browse-root | 1589/1516 | 116 | 0.022 | 0.192 | 172 | index-BBII0rmU.js | 0 | 117 | — |
| browse-unicode | 1704/1621 | 148 | 0.019 | 0.173 | 172 | index-BBII0rmU.js | 77 | 96 | — |
| filter-builder | 1816/1810 | 132 | 0.061 | 0.848 | 247 | index-BBII0rmU.js | 0 | 400 | tablet 1702>768 |
| help | 1536/1510 | 124 | 0.057 | 0.331 | 177 | index-BBII0rmU.js | 0 | 329 | — |
| jobs | 1749/1690 | 168 | 0.795 | 1.392 | 241 | JobsPage-CUwRkXGO.js | 0 | 967 | — |
| kit | 1574/1588 | 164 | 0.057 | 0.345 | 242 | KitPage-BNJkVo68.js | 0 | 511 | — |
| libraries | 1648/1601 | 140 | 0.66 | 1.073 | 279 | LibrariesPage-Yhr4r4JG.js | 0 | 247 | — |
| login | 1551/1539 | 116 | 0.037 | 0.066 | 235 | SearchPage-CLpQGu3F.js | 0 | 38 | — |
| nope | 1561/1613 | 100 | 0.194 | 0.31 | 235 | SearchPage-CLpQGu3F.js | 0 | 101 | — |
| reports | 1572/1558 | 188 | 0.697 | 0.853 | 283 | index-BBII0rmU.js | 0 | 201 | — |
| search | 1888/1858 | 296 | 0.194 | 0.312 | 235 | SearchPage-CLpQGu3F.js | 123 | 101 | — |
| taxonomy | 1695/1613 | 284 | 0.112 | 0.443 | 168 | index-BBII0rmU.js | 0 | 3424 | — |
| timeline | 1509/1499 | 68 | 0.027 | 0.156 | 157 | index-BBII0rmU.js | 0 | 79 | — |

## Failed requests per route (desktop-light)

| Route | Failed requests | API calls |
|---|---|---|
| about | 0:  | 4 |
| account | 0:  | 5 |
| admin | 0:  | 22 |
| agents | 0:  | 34 |
| alerts | 0:  | 6 |
| browse-misc | 10: 404 GET items/7fb0419d-fa48-4c02-b8bc-b5dae3f46d ×1; 404 GET items/48a6ef1c-b1dd-4957-929f-86891777e8 ×1; 404 GET items/17423138-ec43-4729-930d-4799c77618 ×1; 404 GET items/c98636d0-85ee-4a7c-b0de-afa5b7ae3c ×1 | 15 |
| browse-nested | 2: 404 GET items/8cf0f307-181e-452b-b136-6199ce1f37 ×1; 404 GET items/30636a55-2ba0-45e8-8758-d6390c9cc6 ×1 | 7 |
| browse-root | 0:  | 5 |
| browse-unicode | 1: 404 GET items/c91846ee-af32-4dba-8729-6653c92d93 ×1 | 6 |
| filter-builder | 0:  | 6 |
| help | 0:  | 3 |
| jobs | 0:  | 10 |
| kit | 0:  | 3 |
| libraries | 0:  | 15 |
| login | 6: 401 GET auth/me ×1; 401 GET libraries ×1; 401 GET saved-searches ×1; 401 GET taxonomy ×1 | 8 |
| nope | 0:  | 7 |
| reports | 0:  | 14 |
| search | 36: 404 GET items/30636a55-2ba0-45e8-8758-d6390c9cc6 ×1; 404 GET items/8cf0f307-181e-452b-b136-6199ce1f37 ×1; 404 GET items/c98636d0-85ee-4a7c-b0de-afa5b7ae3c ×3; 404 GET items/d43d7b07-2a56-49cb-b4ea-dc6df6c9b0 ×3 | 82 |
| taxonomy | 0:  | 4 |
| timeline | 0:  | 5 |
