# Changelog

All notable changes to AviationLog. Newest version on top.
This file is also shown on the "About" page; the top version is the app version.

## 0.2.4 (2026-11-08)
- Aircraft, fleets and airports lists can be sorted by clicking a column header: first click ascending, second click descending, third click back to the default order; a small arrow shows the direction
- The aircraft list sorts on the server, so the order applies across all pages and stays when you search or change the page; the "Sort" dropdown is replaced by the column headers

## 0.2.3 (2026-11-08)
- Photos: "Remove entry" button in the scan review; the folder on the NAS stays and comes back with the next scan
- Photos: the scan button is now called "Scan archive for new photos"; the buttons explain on mouseover what they do

## 0.2.2 (2026-11-07)
- Photo scan: invisible characters in folder names are ignored, e.g. a trailing space or dot that macOS stores on the NAS as a special character (shown as a box), zero-width characters, and decomposed umlauts
- Photo scan: folders without the type folder (`<Airline>/<REG Airline Type>/<date>`) are recognised, the type is taken from the aircraft folder; an airline abbreviation such as "MGA" for "Mavi Gök Airlines" is no longer taken as christening name

## 0.2.1 (2026-11-07)
- Fix: the photo scan failed completely ("surrogates not allowed") when a folder or file name on the NAS was not valid UTF-8, e.g. umlauts saved by an old program. Such names are now skipped and listed after the scan so you can rename them

## 0.2 (2026-11-07)
- "Registration unknown": aircraft whose registration you don't know can be added anyway, in the photo scan review (one checkbox per folder) and in "+ Sighting". They get a placeholder (?-1, ?-2 …), are not counted in fleet completion, and get their real registration later on the aircraft's edit page; if that registration already exists, the sightings and photos move there
- Photo scan: German military serials such as "15+11" are recognised as registrations
- Photo scan: a registration or military serial entered as a correction is accepted in any format (e.g. MM62243, 11-5101), so such folders can be applied

## 0.1.3 (2026-11-07)
- OpenSky notice: the aircraft database is unlicensed and provided "as is"; the notice and the paper OpenSky asks to cite are shown as an info box at "More about the source" (Master data) and at OpenSky Network (About), on hover, keyboard focus or tap
- Corrected the OpenSky license line: unlicensed, provided "as is"

## 0.1.2 (2026-11-07)
- Fix: map tiles from OpenStreetMap failed with 403 when the reverse proxy sets a strict Referrer-Policy; the map now always sends the origin as Referer

## 0.1.1 (2026-11-07)
- Fix: the OpenSky import failed with "ON CONFLICT DO UPDATE command cannot affect row a second time" when the file lists a registration more than once; such entries are now merged
- README: Traefik setup with the proxy network

## 0.1 (2026-11-07)
- First version: aircraft, sightings and photos with search across all fields
- Master data import from OpenSky, OurAirports and OpenFlights with review before applying; values edited by hand are protected
- Photos in two modes: upload (root server) or read-only scan of a NAS photo archive with review and corrections
- Previews for JPG, PNG, HEIF/HIF and Sony ARW, camera data (EXIF) per photo
- Fleet completion per airline ("X of Y") with inactive marking
- Spotting locations per airport and a map (OpenStreetMap)
- Dashboard with statistics, CSV export
- Users with admin and viewer roles, two-factor login with recovery codes, password reset by e-mail
- Daily database backups with restore, installable as an app on the phone, light and dark mode, English and German
