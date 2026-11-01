# axe-core 4.10.3 summary

100 scans (desktop 1440 + phone 390, light + dark, plus modal states).

| Rule | Impact | Scans affected | Total nodes | Help |
|---|---|---|---|---|
| `select-name` | critical | 16/100 | 60 | Select element must have an accessible name |
| `color-contrast` | serious | 74/100 | 2450 | Elements must meet minimum color contrast ratio thresholds |
| `nested-interactive` | serious | 12/100 | 148 | Interactive controls must not be nested |
| `scrollable-region-focusable` | serious | 10/100 | 10 | Scrollable region must have keyboard access |
| `label-title-only` | serious | 4/100 | 8 | Form elements should have a visible label |
| `heading-order` | moderate | 24/100 | 24 | Heading levels should only increase by one |
| `landmark-one-main` | moderate | 4/100 | 4 | Document should have one main landmark |
| `region` | moderate | 4/100 | 20 | All page content should be contained by landmarks |
| `empty-table-header` | minor | 20/100 | 32 | Table header text should not be empty |

## Per rule detail

### select-name (critical)
Select element must have an accessible name

Routes/states: admin, filter-builder, libraries, libraries-edit-modal

Examples:
- `.grid-cols-1 > select:nth-child(1)` — `<select class="rounded border px-2 py-1 text-sm"><option value="group">group</option><option value="principal">user</option></select>`
- `.grid-cols-1 > select:nth-child(2)` — `<select class="rounded border px-2 py-1 text-sm"><option value="">subject…</option><!----><!----></select>`
- `.p-2 > select:nth-child(2)` — `<select class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700">`
- `select:nth-child(3)` — `<select class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700">`

### color-contrast (serious)
Elements must meet minimum color contrast ratio thresholds

Routes/states: about, account, admin, agents, alerts, browse-misc, browse-nested, browse-root, browse-unicode, filter-builder, help, jobs, kit, kit-modal, libraries, libraries-edit-modal, login, nope, reports, search, search-detail, search-filters, search-results-list, taxonomy, timeline

Examples:
- `a[href$="#/search"]` — `<a href="#/search" class="rounded-lg px-3 py-1 text-sm text-slate-500">Search</a>`
- `a[href$="#/libraries"]` — `<a href="#/libraries" class="rounded-lg px-3 py-1 text-sm text-slate-500">Libraries</a>`
- `.text-amber-600` — `<span class="text-amber-600 dark:text-amber-400">amber</span>`
- `nav > a[href$="#/libraries"]` — `<a href="#/libraries" class="rounded-lg px-3 py-1 text-sm text-slate-500">Libraries</a>`

### nested-interactive (serious)
Interactive controls must not be nested

Routes/states: search-detail, search-filters, search-results-list

Examples:
- `#opt-d43d7b07-2a56-49cb-b4ea-dc6df6c9b02f` — `<div role="option" tabindex="-1" id="opt-d43d7b07-2a56-49cb-b4ea-dc6df6c9b02f" aria-selected="true" class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left bg-[var(--accent)]/10 ring-`
- `#opt-d485cba6-0bd2-41e3-8edf-7634647fdc59` — `<div role="option" tabindex="-1" id="opt-d485cba6-0bd2-41e3-8edf-7634647fdc59" aria-selected="false" class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left hover:bg-slate-50 dark:hov`
- `#opt-30636a55-2ba0-45e8-8758-d6390c9cc644` — `<div role="option" tabindex="-1" id="opt-30636a55-2ba0-45e8-8758-d6390c9cc644" aria-selected="true" class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left bg-[var(--accent)]/10 ring-`
- `#opt-8cf0f307-181e-452b-b136-6199ce1f376f` — `<div role="option" tabindex="-1" id="opt-8cf0f307-181e-452b-b136-6199ce1f376f" aria-selected="false" class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left hover:bg-slate-50 dark:hov`

### scrollable-region-focusable (serious)
Scrollable region must have keyboard access

Routes/states: admin, jobs, libraries, libraries-edit-modal

Examples:
- `.mt-8:nth-child(10) > .overflow-x-auto.mt-3` — `<div class="mt-3 overflow-x-auto">`
- `.max-h-96` — `<div class="max-h-96 overflow-y-auto font-mono text-xs">`
- `section:nth-child(4) > .overflow-x-auto.mt-3` — `<div class="mt-3 overflow-x-auto">`
- `.mt-8:nth-child(4) > .overflow-x-auto.mt-3` — `<div class="mt-3 overflow-x-auto">`

### label-title-only (serious)
Form elements should have a visible label

Routes/states: jobs

Examples:
- `select[title="Minimum level"]` — `<select class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700" title="Minimum level"><option value="info">info+</option><option value="warning">warning+</opt`
- `select[title="Which process"]` — `<select class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700" title="Which process"><option value="">app + worker</option><option value="app">app</option><o`

### heading-order (moderate)
Heading levels should only increase by one

Routes/states: account, browse-misc, browse-nested, browse-root, browse-unicode, search-detail

Examples:
- `section:nth-child(1) > h4` — `<h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Profile</h4>`
- `h3` — `<h3 class="mb-2 text-sm font-semibold">Permissions</h3>`

### landmark-one-main (moderate)
Document should have one main landmark

Routes/states: login

Examples:
- `html` — `<html lang="en" style="--accent: #4f46e5; --accent-fg: #ffffff;">`

### region (moderate)
All page content should be contained by landmarks

Routes/states: login

Examples:
- `h1` — `<h1 class="mb-1 text-2xl font-bold" style="color: var(--accent)">Filearr</h1>`
- `p` — `<p class="mb-4 text-sm text-slate-500">Sign in to continue.</p>`

### empty-table-header (minor)
Table header text should not be empty

Routes/states: admin, agents, filter-builder, libraries, libraries-edit-modal

Examples:
- `.mt-8:nth-child(2) > .overflow-x-auto.mt-3 > table > .uppercase > tr > th:nth-child(5)` — `<th class="py-1 pr-3"></th>`
- `.overflow-x-auto:nth-child(5) > table > thead > .text-left > th:nth-child(4)` — `<th></th>`
- `.p-4 > .overflow-x-auto > table > thead > tr > th:nth-child(5)` — `<th></th>`
- `th:nth-child(5)` — `<th class="px-2 py-1.5 font-medium"></th>`

## Per scan violation counts

| Scan | violations (rules) | nodes |
|---|---|---|
| about-dark-desktop | 1 | 116 |
| about-dark-phone | 1 | 59 |
| about-light-desktop | 1 | 1 |
| about-light-phone | 1 | 1 |
| account-dark-desktop | 2 | 46 |
| account-dark-phone | 2 | 45 |
| account-light-desktop | 1 | 1 |
| account-light-phone | 1 | 1 |
| admin-dark-desktop | 3 | 115 |
| admin-dark-phone | 4 | 83 |
| admin-light-desktop | 3 | 15 |
| admin-light-phone | 4 | 16 |
| agents-dark-desktop | 2 | 70 |
| agents-dark-phone | 2 | 62 |
| agents-light-desktop | 2 | 3 |
| agents-light-phone | 2 | 2 |
| alerts-dark-desktop | 1 | 21 |
| alerts-dark-phone | 1 | 20 |
| alerts-light-desktop | 0 | 0 |
| alerts-light-phone | 0 | 0 |
| browse-misc-dark-desktop | 2 | 22 |
| browse-misc-dark-phone | 2 | 21 |
| browse-misc-light-desktop | 1 | 1 |
| browse-misc-light-phone | 1 | 1 |
| browse-nested-dark-desktop | 2 | 23 |
| browse-nested-dark-phone | 2 | 22 |
| browse-nested-light-desktop | 1 | 1 |
| browse-nested-light-phone | 1 | 1 |
| browse-root-dark-desktop | 2 | 21 |
| browse-root-dark-phone | 2 | 20 |
| browse-root-light-desktop | 1 | 1 |
| browse-root-light-phone | 1 | 1 |
| browse-unicode-dark-desktop | 2 | 23 |
| browse-unicode-dark-phone | 2 | 22 |
| browse-unicode-light-desktop | 1 | 1 |
| browse-unicode-light-phone | 1 | 1 |
| filter-builder-dark-desktop | 3 | 24 |
| filter-builder-dark-phone | 3 | 23 |
| filter-builder-light-desktop | 3 | 4 |
| filter-builder-light-phone | 3 | 4 |
| help-dark-desktop | 1 | 23 |
| help-dark-phone | 1 | 22 |
| help-light-desktop | 1 | 1 |
| help-light-phone | 1 | 1 |
| jobs-dark-desktop | 3 | 146 |
| jobs-dark-phone | 3 | 105 |
| jobs-light-desktop | 3 | 15 |
| jobs-light-phone | 3 | 15 |
| kit-dark-desktop | 1 | 18 |
| kit-dark-phone | 1 | 17 |
| kit-light-desktop | 0 | 0 |
| kit-light-phone | 0 | 0 |
| kit-modal-dark-desktop | 1 | 18 |
| kit-modal-dark-phone | 1 | 17 |
| kit-modal-light-desktop | 0 | 0 |
| kit-modal-light-phone | 0 | 0 |
| libraries-dark-desktop | 3 | 64 |
| libraries-dark-phone | 4 | 49 |
| libraries-edit-modal-dark-desktop | 3 | 101 |
| libraries-edit-modal-dark-phone | 4 | 81 |
| libraries-edit-modal-light-desktop | 3 | 11 |
| libraries-edit-modal-light-phone | 4 | 6 |
| libraries-light-desktop | 3 | 10 |
| libraries-light-phone | 4 | 4 |
| login-dark-desktop | 3 | 9 |
| login-dark-phone | 3 | 9 |
| login-light-desktop | 2 | 6 |
| login-light-phone | 2 | 6 |
| nope-dark-desktop | 1 | 24 |
| nope-dark-phone | 1 | 23 |
| nope-light-desktop | 0 | 0 |
| nope-light-phone | 0 | 0 |
| reports-dark-desktop | 1 | 47 |
| reports-dark-phone | 1 | 46 |
| reports-light-desktop | 0 | 0 |
| reports-light-phone | 0 | 0 |
| search-dark-desktop | 1 | 24 |
| search-dark-phone | 1 | 23 |
| search-detail-dark-desktop | 3 | 67 |
| search-detail-dark-phone | 3 | 49 |
| search-detail-light-desktop | 3 | 21 |
| search-detail-light-phone | 3 | 19 |
| search-filters-dark-desktop | 2 | 85 |
| search-filters-dark-phone | 2 | 68 |
| search-filters-light-desktop | 2 | 20 |
| search-filters-light-phone | 2 | 18 |
| search-light-desktop | 0 | 0 |
| search-light-phone | 0 | 0 |
| search-results-list-dark-desktop | 2 | 30 |
| search-results-list-dark-phone | 2 | 27 |
| search-results-list-light-desktop | 2 | 6 |
| search-results-list-light-phone | 2 | 5 |
| taxonomy-dark-desktop | 1 | 176 |
| taxonomy-dark-phone | 1 | 175 |
| taxonomy-light-desktop | 1 | 47 |
| taxonomy-light-phone | 1 | 47 |
| timeline-dark-desktop | 1 | 21 |
| timeline-dark-phone | 1 | 20 |
| timeline-light-desktop | 0 | 0 |
| timeline-light-phone | 0 | 0 |
