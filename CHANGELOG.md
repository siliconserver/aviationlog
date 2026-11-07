# Changelog

All notable changes to AviationLog. Newest version on top.
This file is also shown on the "About" page; the top version is the app version.

## 0.1.4 (2026-11-07)
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
