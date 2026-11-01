# Keyboard pass (desktop 1440x900, light)

Each run: load route, press Tab 40 times, record document.activeElement. "no visible focus ring?" = computed outline none AND no box-shadow at that moment (heuristic; Tailwind focus-visible rings use box-shadow).

## search

Start focus: BODY

| # | activeElement |
|---|---|
| 1 | a "Skip to main content" |
| 2 | a "Search" |
| 3 | a "Libraries" |
| 4 | a "Admin" |
| 5 | a "Jobs" |
| 6 | a "Alerts" |
| 7 | a "Timeline" |
| 8 | a "Reports" |
| 9 | a "Agents" |
| 10 | a "Filter builder" |
| 11 | a "admin(admin)" |
| 12 | button[submit] "Sign out" |
| 13 | select[select-one] "Network path format" |
| 14 | button[submit] "Toggle dark theme" |
| 15 | input[text] "report" |
| 16 | button[submit] "all" |
| 17 | button[submit] "Image" |
| 18 | button[submit] "Audio" |
| 19 | button[submit] "Video & subtitle" |
| 20 | button[submit] "Document2" |
| 21 | button[submit] "3D & CAD" |
| 22 | button[submit] "Development & data" |
| 23 | button[submit] "Archive & image" |
| 24 | button[submit] "System & data files" |
| 25 | button[submit] "Other / unknown" |
| 26 | select[select-one] "Sort results" |
| 27 | button[button] "List" |
| 28 | button[button] "Grid" |
| 29 | button[button] "Map" |
| 30 | button[submit] "Saved ▾" |
| 31 | button[submit] "Filters ▾" |
| 32 | button[button] "Query syntax ▾" |
| 33 | button[button] "Select all loaded (2)" |
| 34 | div[role=listbox] "Search results" |
| 35 | input[checkbox] "Select annual report 2025.txt" |
| 36 | button[button] "Copy path" |
| 37 | input[checkbox] "Select Q3 résumé – final.md" |
| 38 | button[button] "Copy path" |
| 39 | a "Help" |
| 40 | a "About" |

Unique stops in 40 tabs: 39. Missing focus ring count: 0.

Screenshot: shots/keyboard-search-desktop-light.png

## libraries-edit-modal

Start focus: button[submit] "Edit" (no visible focus ring?)

| # | activeElement |
|---|---|
| 1 | button[submit] "Diagnose" |
| 2 | button[submit] "Delete" |
| 3 | button[submit] "Scan" |
| 4 | button[submit] "Add library…" |
| 5 | button[button] "Help: custom field name" |
| 6 | input[text] "e.g. shelf_location" |
| 7 | input[text] "Human-readable label" |
| 8 | button[button] "Help: custom field type" |
| 9 | select[select-one] "string integer float boolean date url se" |
| 10 | button[button] "Help: custom field libraries" |
| 11 | button[button] "Sample Media" |
| 12 | input[checkbox] "on" |
| 13 | input[checkbox] "on" |
| 14 | button[button] "Help: facet/sort" |
| 15 | input[checkbox] "on" |
| 16 | button[button] "Help: required" |
| 17 | button[submit] "Add custom field" |
| 18 | button[submit] "New preset" |
| 19 | button[submit] "fork" |
| 20 | button[submit] "fork" |
| 21 | button[submit] "fork" |
| 22 | button[submit] "fork" |
| 23 | button[submit] "fork" |
| 24 | button[button] "Close editor" |
| 25 | button[submit] "Close" (in dialog) |
| 26 | button[button] "Help: name" (in dialog) |
| 27 | input[text] "Sample Media" (in dialog) |
| 28 | button[button] "Help: root path" (in dialog) |
| 29 | input[text] "/tmp/claude-0/-home-user/6189c784-cfba-5" (in dialog) |
| 30 | button[button] "Browse…" (in dialog) |
| 31 | button[button] "Help: native prefix" (in dialog) |
| 32 | input[text] "(optional) e.g. /mnt/user/media" (in dialog) |
| 33 | button[button] "Help: share location" (in dialog) |
| 34 | input[text] "(optional) e.g. \towermedia, smb://tower" (in dialog) |
| 35 | button[button] "Help: enabled" (in dialog) |
| 36 | input[checkbox] "on" (in dialog) |
| 37 | button[button] "Help: file types" (in dialog) |
| 38 | input[checkbox] "on" (in dialog) |
| 39 | button[button] "groups (5)" (in dialog) |
| 40 | input[checkbox] "on" (in dialog) |

Unique stops in 40 tabs: 32. Missing focus ring count: 0. Focus **ESCAPED the dialog (focus trap broken)**.

After Escape: dialog closed; focus on: BODY

Screenshot: shots/keyboard-libraries-edit-modal-desktop-light.png

## kit

Start focus: BODY

| # | activeElement |
|---|---|
| 1 | a "Skip to main content" |
| 2 | a "Search" |
| 3 | a "Libraries" |
| 4 | a "Admin" |
| 5 | a "Jobs" |
| 6 | a "Alerts" |
| 7 | a "Timeline" |
| 8 | a "Reports" |
| 9 | a "Agents" |
| 10 | a "Filter builder" |
| 11 | a "admin(admin)" |
| 12 | button[submit] "Sign out" |
| 13 | select[select-one] "Network path format" |
| 14 | button[submit] "Toggle dark theme" |
| 15 | button[button] "Save changes" |
| 16 | button[button] "Cancel" |
| 17 | button[button] "Delete" |
| 18 | button[button] "Skip" |
| 19 | button[button] "Save changes" |
| 20 | button[button] "Cancel" |
| 21 | button[button] "Delete" |
| 22 | button[button] "Skip" |
| 23 | a "Open help" |
| 24 | a "Secondary link" |
| 25 | a "Ghost link" |
| 26 | button[button] "Dismiss" |
| 27 | button[button] "Retry" |
| 28 | input[text] "Movies" |
| 29 | input[text] "movies" |
| 30 | select[select-one] "Hourly Nightly Weekly Manual only" |
| 31 | textarea[textarea] "Who owns this share…" |
| 32 | input[text] "size="sm"" |
| 33 | button[button] "Open sm" |
| 34 | button[button] "Open md" |
| 35 | button[button] "Open lg" |
| 36 | button[button] "Open xl" |
| 37 | button[button] "Rescan library…" |
| 38 | button[button] "Delete library…" |
| 39 | button[button] "Info" |
| 40 | button[button] "Ok" |

Unique stops in 40 tabs: 36. Missing focus ring count: 0.

Screenshot: shots/keyboard-kit-desktop-light.png

## kit-modal

Start focus: input[text] "Movies" (in dialog)

| # | activeElement |
|---|---|
| 1 | button[button] "Cancel" (in dialog) |
| 2 | button[button] "Save" (in dialog) |
| 3 | button[submit] "Close" (in dialog) |
| 4 | input[text] "Movies" (in dialog) |
| 5 | button[button] "Cancel" (in dialog) |
| 6 | button[button] "Save" (in dialog) |
| 7 | button[submit] "Close" (in dialog) |
| 8 | input[text] "Movies" (in dialog) |
| 9 | button[button] "Cancel" (in dialog) |
| 10 | button[button] "Save" (in dialog) |
| 11 | button[submit] "Close" (in dialog) |
| 12 | input[text] "Movies" (in dialog) |
| 13 | button[button] "Cancel" (in dialog) |
| 14 | button[button] "Save" (in dialog) |
| 15 | button[submit] "Close" (in dialog) |
| 16 | input[text] "Movies" (in dialog) |
| 17 | button[button] "Cancel" (in dialog) |
| 18 | button[button] "Save" (in dialog) |
| 19 | button[submit] "Close" (in dialog) |
| 20 | input[text] "Movies" (in dialog) |
| 21 | button[button] "Cancel" (in dialog) |
| 22 | button[button] "Save" (in dialog) |
| 23 | button[submit] "Close" (in dialog) |
| 24 | input[text] "Movies" (in dialog) |
| 25 | button[button] "Cancel" (in dialog) |
| 26 | button[button] "Save" (in dialog) |
| 27 | button[submit] "Close" (in dialog) |
| 28 | input[text] "Movies" (in dialog) |
| 29 | button[button] "Cancel" (in dialog) |
| 30 | button[button] "Save" (in dialog) |
| 31 | button[submit] "Close" (in dialog) |
| 32 | input[text] "Movies" (in dialog) |
| 33 | button[button] "Cancel" (in dialog) |
| 34 | button[button] "Save" (in dialog) |
| 35 | button[submit] "Close" (in dialog) |
| 36 | input[text] "Movies" (in dialog) |
| 37 | button[button] "Cancel" (in dialog) |
| 38 | button[button] "Save" (in dialog) |
| 39 | button[submit] "Close" (in dialog) |
| 40 | input[text] "Movies" (in dialog) |

Unique stops in 40 tabs: 4. Missing focus ring count: 0. Focus stayed inside the dialog (trap OK).

After Escape: dialog closed; focus on: button[button] "Open md"

Screenshot: shots/keyboard-kit-modal-desktop-light.png

## search-detail-modal

Start focus: div[role=option] "document annual report 2025.txt Annual r"

| # | activeElement |
|---|---|
| 1 | input[checkbox] "Select annual report 2025.txt" |
| 2 | button[button] "Copy path" |
| 3 | input[checkbox] "Select Q3 résumé – final.md" |
| 4 | button[button] "Copy path" |
| 5 | button[button] "Close details" |
| 6 | button[button] "Sample Media" (in dialog) |
| 7 | button[button] "Path actions for Sample Media" (in dialog) |
| 8 | button[button] "Documents" (in dialog) |
| 9 | button[button] "Path actions for Documents" (in dialog) |
| 10 | button[button] "Reports" (in dialog) |
| 11 | button[button] "Path actions for Reports" (in dialog) |
| 12 | button[button] "Path actions for annual report 2025.txt" (in dialog) |
| 13 | button[button] "Copy path" (in dialog) |
| 14 | button[submit] "Document" (in dialog) |
| 15 | button[submit] "Raw" (in dialog) |
| 16 | button[submit] "Edit" (in dialog) |
| 17 | button[submit] "Close" (in dialog) |
| 18 | a "Help" |
| 19 | a "About" |
| 20 | a "Docs" |
| 21 | a "API Docs" |
| 22 | a "Source" |
| 23 | BODY |
| 24 | a "Skip to main content" |
| 25 | a "Search" |
| 26 | a "Libraries" |
| 27 | a "Admin" |
| 28 | a "Jobs" |
| 29 | a "Alerts" |
| 30 | a "Timeline" |
| 31 | a "Reports" |
| 32 | a "Agents" |
| 33 | a "Filter builder" |
| 34 | a "admin(admin)" |
| 35 | button[submit] "Sign out" |
| 36 | select[select-one] "Network path format" |
| 37 | button[submit] "Toggle dark theme" |
| 38 | input[text] "report" |
| 39 | button[submit] "all" |
| 40 | button[submit] "Image" |

Unique stops in 40 tabs: 39. Missing focus ring count: 0. Focus **ESCAPED the dialog (focus trap broken)**.

After Escape: dialog closed; focus on: button[submit] "Image"

Screenshot: shots/keyboard-search-detail-modal-desktop-light.png

## Skip link (#/jobs)

First Tab: a "Skip to main content"; after Enter: main "Jobs updated 12:09:46 AM Enter maintenan" (no visible focus ring?)
