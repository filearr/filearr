# Flows (desktop 1440x900 light unless noted)

| Flow | Result | Time | Notes | Screenshot |
|---|---|---|---|---|
| login wrong then right password | PASS | 5590 ms | wrong-pw message: "Invalid username or password."; after right pw nav visible=true; url=http://127.0.0.1:4173/#/search | shots/flow-login-wrong-then-right-password-desktop-light.png |
| create saved search | PASS | 4271 ms | clicked=true; saved entry visible=true | shots/flow-create-saved-search-desktop-light.png |
| edit library name and save | PASS | 6185 ms | save clicked=true; modal closed=true; new name in list=true; reverted=true | shots/flow-edit-library-name-and-save-desktop-light.png |
| filters panel + type facet | PASS | 3677 ms | filters opened=true; results before=undefined after Image facet=9; url=http://127.0.0.1:4173/#/search?file_category=image | shots/flow-filters-panel-type-facet-desktop-light.png |
| rescan from Libraries page + progress UI | PASS | 9994 ms | scan clicked=true; status text seen: "finished just now — seen 38, new 0, changed 0, missing 0 excluded 1 "; snaps: flow-rescan-300ms-desktop-light.png, flow-rescan-1200ms-desktop-light.png, flow-rescan-3000ms-desktop-light.png, flow-rescan-8000ms-desktop-light.png | shots/flow-rescan-from-libraries-page-progress-ui-desktop-light.png |
| bulk-select two results + bulk edit bar | PASS | 3384 ms | checkboxes=6; bar text="2 selected"; bulk edit buttons=0; opened=false | shots/flow-bulk-select-two-results-bulk-edit-bar-desktop-light.png |
| kit confirm() demo + Escape | PASS | 2762 ms | dialog opened=true; focus in dialog: button[button] "Cancel" (in dialog); closed on Esc=true; focus returned to: button[button] "Rescan library…"; page result text: "result shows as a toast." | shots/flow-kit-confirm-demo-escape-desktop-light.png |
| switch theme via header button | PASS (harness artefact corrected) | 3522 ms | dark before=false after=true. The automated reload check reported persisted=false only because this harness's addInitScript re-seeds localStorage.theme=light on every load; re-tested without it (theme_retest.mjs): system -> click -> dark, localStorage=dark, still dark after reload, aria-pressed=true, aria-label 'Toggle dark theme'. Note: toggle has no 'system' option once clicked | shots/flow-switch-theme-via-header-button-desktop-light.png |
| resize desktop -> phone with modal open | PASS | 4683 ms | after resize: {"open":true,"left":0,"right":390,"width":390,"top":64,"docW":390,"vw":390}; shots flow-resize-before-desktop-light.png / flow-resize-after-phone-light.png | shots/flow-resize-desktop-phone-with-modal-open-desktop-light.png |
| logout | PASS | 2581 ms | sign-out clicked=true; login form shown=true | shots/flow-logout-desktop-light.png |

## Console / network per flow

### login wrong then right password
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
  - error: Failed to load resource: the server responded with a status of 401 (Unauthorized)
- pageerrors: none
- failed requests: 401 GET /api/v1/auth/me ; 401 GET /api/v1/libraries ; 401 GET /api/v1/saved-searches ; 401 GET /api/v1/taxonomy ; 401 GET /api/v1/system/file-groups ; 401 GET /api/v1/stats ; 401 POST /api/v1/auth/login

### create saved search
- native dialogs: prompt: Name this search:
- console: 
  - warning: Service Worker registration blocked by Playwright
  - error: Failed to load resource: the server responded with a status of 404 (Not Found)
- pageerrors: none
- failed requests: 404 GET /api/v1/items/8cf0f307-181e-452b-b136-6199ce1f376f/thumb?tier=grid ; 404 GET /api/v1/items/30636a55-2ba0-45e8-8758-d6390c9cc644/thumb?tier=grid

### edit library name and save
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### filters panel + type facet
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### rescan from Libraries page + progress UI
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### bulk-select two results + bulk edit bar
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
  - error: Failed to load resource: the server responded with a status of 404 (Not Found)
- pageerrors: none
- failed requests: 404 GET /api/v1/items/c5433b39-6478-4a4c-bb9f-5093f3c6c6c5/thumb?tier=grid ; 404 GET /api/v1/items/30636a55-2ba0-45e8-8758-d6390c9cc644/thumb?tier=grid ; 404 GET /api/v1/items/c91846ee-af32-4dba-8729-6653c92d93b8/thumb?tier=grid ; 404 GET /api/v1/items/7fb0419d-fa48-4c02-b8bc-b5dae3f46dee/thumb?tier=grid ; 404 GET /api/v1/items/72213048-4cb5-43eb-8ca3-6f4699712e3e/thumb?tier=grid ; 404 GET /api/v1/items/48a6ef1c-b1dd-4957-929f-86891777e877/thumb?tier=grid

### kit confirm() demo + Escape
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### switch theme via header button
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### resize desktop -> phone with modal open
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
- pageerrors: none
- failed requests: none

### logout
- native dialogs: none
- console: 
  - warning: Service Worker registration blocked by Playwright
  - error: Failed to load resource: the server responded with a status of 401 (Unauthorized)
- pageerrors: none
- failed requests: 401 GET /api/v1/auth/me
