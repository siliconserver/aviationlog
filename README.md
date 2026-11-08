# AviationLog

Self-hosted web app for plane spotters, also available as a [desktop app](#desktop-app) for Windows, macOS and Debian: log the aircraft you photographed, find them again by any detail, see how much of each airline's fleet you already have, and keep your spotting locations on a map.

You only enter **registration, date and airport**. Type, airline, MSN, build year, engines and more come from the built-in master data, imported from free public sources.

## Features

- **Aircraft and sightings**: one entry per registration with any number of sightings (date, airport, spotting location, livery, photos). "Seen" is the number of sightings. Re-registered airframes are linked through the MSN.
- **Search** across all details (registration, MSN, type, airline, livery, christening name, airport, spotting location, camera, lens), optionally limited to one field.
- **Master data import** from OpenSky (aircraft), OurAirports (airports) and OpenFlights (airlines, type codes), plus a lookup of single aircraft at adsbdb.com. Every import is shown for review first: new and changed records, with the option to correct or reject them. Values you edited by hand are never overwritten.
- **Fleet completion**: per airline, spotted active aircraft out of the whole active fleet ("X of Y"). Retired aircraft can be marked inactive so they no longer count. The overview lists airlines with at least one spotted aircraft; "Show all airlines" lists every airline in the database.
- **Your own master data**: airlines and airports can be edited (e.g. to add a missing IATA or ICAO code) and added by hand, and aircraft not spotted yet can be added to a fleet. A later import merges a hand-made airline or airport with the entry of the same ICAO code and keeps your values. Airlines and airports without sightings can be deleted.
- **Photos** in two modes (see below), previews for JPG, PNG, HEIF/HIF and Sony ARW, camera data (EXIF: camera, lens, focal length, aperture, exposure, ISO, GPS) per photo, a cover photo per aircraft.
- **Map** (OpenStreetMap) with named spotting locations per airport, set by clicking on the map. Photos with GPS data are assigned to the nearest spotting location automatically.
- **Dashboard** with statistics per airline, type, airport, year, month, camera and lens. **CSV export** of the whole collection.
- **Users** with the roles admin (may change everything) and viewer (read only), two-factor login with an authenticator app plus recovery codes, password reset by e-mail (optional SMTP).
- Daily **database backups** with download and restore in the web UI.
- Installable as an **app on the phone** (PWA), light and dark mode, **English and German** (more languages welcome, see below).

## Quick start

```bash
mkdir aviationlog && cd aviationlog
curl -O https://raw.githubusercontent.com/siliconserver/aviationlog/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/siliconserver/aviationlog/main/.env.example
# edit .env: passwords, SECRET_KEY, photo mode
docker compose up -d
```

Open `http://<server>:8090` and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`. Then:

1. **Admin → Master data**: download and apply airports, airlines, aircraft types and the aircraft database (the OpenSky file is large; the first import takes a few minutes).
2. **Profile**: turn on two-factor authentication.
3. Start logging: **+ Sighting**, or scan your photo archive (NAS mode).

Everything the app stores lives in `./data` next to `docker-compose.yml`. To move to another server, copy the whole folder including `.env`.

### Update

```bash
docker compose pull && docker compose up -d
```

`AVIATIONLOG_TAG` in `.env` selects the image: `latest` (newest release), `main` (current main branch) or a fixed version such as `0.1`.

## Desktop app

For a single computer without Docker there are ready-made apps for **Windows** (64 bit), **macOS** (Apple silicon) and **Debian/Ubuntu** (amd64). Download them from the assets of the newest [release](https://github.com/siliconserver/aviationlog/releases/latest):

| System | File | Install |
| --- | --- | --- |
| Windows 10/11 | `AviationLog-<version>-windows-x64-setup.exe` | Run the installer (no administrator rights needed). Windows SmartScreen may warn because the app is not signed: *More info → Run anyway*. |
| macOS 12 or newer, Apple silicon | `AviationLog-<version>-macos-arm64.dmg` | Drag AviationLog into *Applications*. The app is not notarized by Apple: on the first start right-click it and choose *Open*, or allow it under *System Settings → Privacy & Security*. |
| Debian 12+, Ubuntu 22.04+ | `aviationlog_<version>_amd64.deb` | `sudo apt install ./aviationlog_<version>_amd64.deb` (also installs WebKitGTK for the window), then start AviationLog from the app menu or with `aviationlog`. |

The desktop app is the same AviationLog with its own PostgreSQL database built in. It opens in its own window, no browser needed; it uses the web view of the system (Edge WebView2 on Windows, WebKit on macOS and Linux). Closing the window stops AviationLog and the database. In the background it runs at `http://127.0.0.1:18090`, only reachable from this computer; *AviationLog → Open in browser* opens it in a browser tab as well.

- **First login**: the login page shows the user `admin` and a generated start password until you change it under *Profile*.
- **Photos**: by default photos are uploaded with each sighting and stored in the data folder. With *Photos → Use photo archive …* you pick an existing archive folder in the [folder scheme](#folder-scheme); it is only read and scanned (like `nas` mode), never changed. *Photos → Store photos in AviationLog* switches back.
- **Downloads** (CSV export, backups, original photos) ask where to save the file. Links to other websites open in your browser.
- **Data folder** (database, backups, thumbnails, uploaded photos, settings, logs):
  - Windows: `%LOCALAPPDATA%\AviationLog`
  - macOS: `~/Library/Application Support/AviationLog`
  - Linux: `~/.local/share/aviationlog`

  Uninstalling the app keeps this folder. Back it up regularly; to move to another computer copy the whole folder.
- **Settings** are in `config.json` in the data folder: `port`, `language` (`en`/`de`), `photo_mode`/`photo_dir`, `window` (`"browser"`: small control window and the normal browser instead of the app window; this is also used automatically when the system has no web view) and optionally `smtp` (e.g. `{"host": "smtp.example.com", "port": 587, "user": "…", "password": "…", "from": "…"}`). Restart the app after changing it.
- Backups of the server version can be restored in the desktop app and the other way round (*Admin → Backups → Upload*).
- Without a desktop (e.g. over SSH) start it with `aviationlog --headless`; Ctrl+C stops it.

## Photo modes

Set `PHOTO_MODE` in `.env`. Photos always come from one direction only, so the file system cannot get mixed up. You can switch later, because both modes use the same folder scheme.

| Mode | Use it for | Behaviour |
| --- | --- | --- |
| `upload` | root server | Photos are uploaded with a sighting and stored in `./data/app/photos` in the folder scheme. |
| `nas` | existing archive on a NAS | The archive is mounted **read only**. *Admin → Photos → Scan* lists new folders; you check and correct the detected values, then apply. Nothing on the NAS is ever written. |

NAS example (`.env`):

```env
PHOTO_MODE=nas
PHOTO_HOST_DIR=/volume1/photos/spotting
PHOTO_MOUNT_OPTS=:ro
```

### Folder scheme

```
<Airline>/<Type ICAO>/<REG Airline Type [Christening name] [(Livery)]>/<YYYY-MM-DD-IATA>/<photos>
```

Examples:

```
Lufthansa/A20N/D-AIJC Lufthansa A20N Lünen/2026-06-05-FRA/DSC01234.ARW
Condor/A339/D-ANRA Condor A339 Green/2026-04-17-FRA/DSC01300.JPG
SunExpress/B738/TC-SOZ SunExpress (Istanbul Livery) B738/2026-06-15-CGN/DSC01400.HIF
```

- The registration is the first word of the aircraft folder.
- Text in brackets is the **livery**, any other text (without airline and type) the **christening name**. If that is wrong for a folder, correct it in the scan review; the correction is remembered for that folder.
- The date folder is `YYYY-MM-DD-` followed by the IATA (or ICAO) airport code.
- Supported files: JPG, PNG, HEIF/HEIC/HIF, ARW. When a RAW and a JPEG/HEIF with the same name exist, the gallery shows the JPEG and offers the RAW as a download.
- A sighting created on the phone before the scan (same registration, date and airport) is matched with its folder, no duplicate is created.

## Behind Traefik

The app listens on port 8000 in the container. Traefik can only reach it if the `app` service is also attached to the Docker network Traefik runs in (called `proxy` here, use the name of your network). Without that you get a *Gateway Timeout*. In `docker-compose.yml` remove the `ports:` section of `app` and add (the same lines are prepared there as comments):

```yaml
  app:
    # ...
    networks:
      - default   # keeps the connection to the database
      - proxy
    labels:
      - traefik.enable=true
      - traefik.docker.network=proxy
      - traefik.http.routers.aviationlog.rule=Host(`aviationlog.example.com`)
      - traefik.http.routers.aviationlog.entrypoints=websecure
      - traefik.http.routers.aviationlog.tls.certresolver=letsencrypt
      - traefik.http.services.aviationlog.loadbalancer.server.port=8000

networks:
  proxy:
    external: true
```

`traefik.docker.network` tells Traefik which of the two networks to use; without it Traefik may pick the internal one and time out as well. Only the `app` service joins `proxy`, the database stays internal.

With HTTPS set `SESSION_HTTPS_ONLY=true` and `BASE_URL=https://aviationlog.example.com` (used in password reset mails). Large photo uploads may need a higher body size limit in your proxy.

## Master data sources

| Source | Content | License |
| --- | --- | --- |
| [OpenSky Network](https://opensky-network.org/data/aircraft) | aircraft: registration, Mode S, type, MSN, operator, partly build year and engines | unlicensed, provided "as is" (see below) |
| [OurAirports](https://ourairports.com/data/) | airports with IATA/ICAO codes and coordinates | Public Domain |
| [OpenFlights](https://openflights.org/data) | airlines and aircraft type codes (IATA/ICAO) | ODbL |
| ICAO Doc 8643 via OpenSky | aircraft type designators, manufacturer, engines | unlicensed, provided "as is" (see below) |
| [adsbdb.com](https://www.adsbdb.com/) (lookup by registration) | aircraft: type, manufacturer, Mode S, operator | free public API, aircraft data courtesy of PlaneBase |

Delivery date, production site and status (active/inactive) are not available from free sources in good quality. Enter them by hand once per aircraft; they are kept across imports. If the container has no internet access, download the files elsewhere and upload them on the same page. The download URLs can be changed with `OPENSKY_URL`, `OURAIRPORTS_URL`, `OPENFLIGHTS_AIRLINES_URL`, `OPENFLIGHTS_PLANES_URL` and `DOC8643_URL`.

**adsbdb.com lookup.** adsbdb completes single aircraft, e.g. ones that are newer than the OpenSky snapshot. It has no MSN, build year or fleets. There are three ways to use it:
- **Load data** on an aircraft page looks up that registration and applies the result.
- New aircraft (sighting form, fleet page, photo scan) are looked up automatically in the background. This can be switched off in *Admin → Master data*.
- *Admin → Master data → Look up all aircraft of the collection* asks for every spotted aircraft and every aircraft added by hand or by the scan, one request every 0.5 seconds. The result is shown for review like any import.

Fields you edited by hand are never changed; model and manufacturer are only filled when empty. `ADSBDB_URL=off` switches all lookups off; `ADSBDB_DELAY` sets the pause between two requests in seconds.

The OpenSky aircraft database is unlicensed and does not fall under OpenSky's terms of use; it comes without support or guarantees of any kind. If you use OpenSky Network data in a publication (including web pages, research papers or public presentations), OpenSky asks you to cite:

> Matthias Schäfer, Martin Strohmeier, Vincent Lenders, Ivan Martinovic, and Matthias Wilhelm. "Bringing Up OpenSky: A Large-scale ADS-B Sensor Network for Research". In Proceedings of the 13th IEEE/ACM International Symposium on Information Processing in Sensor Networks (IPSN), pages 83-94, April 2014.

The app shows this notice in *Admin → Master data* and on the About page. Official website: [The OpenSky Network](https://opensky-network.org/).

AviationLog does not scrape any website. Please respect the licenses of the sources.

## Backups

The database is backed up every day (time and number of kept backups under *Admin → Backups*) to `./data/app/backups`. **Photos are not part of the backup**, back up your photo folder separately. Thumbnails in `./data/app/thumbs` can always be recreated.

## Configuration

All settings are in `.env`, see [.env.example](.env.example) for the full list with comments.

## Translations

The user interface is translated with one JSON file per language in [`app/i18n`](app/i18n). To add a language, copy `en.json` to `<code>.json` (e.g. `fr.json`), translate the values, set `_language_name` and open a pull request. Missing keys fall back to English; the tests check that every language has all keys.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
# a PostgreSQL 16 database is needed, e.g. docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16-alpine
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres DATA_DIR=$PWD/data PHOTO_DIR=$PWD/data/photos
cd app && uvicorn main:app --reload
```

Tests: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/aviationlog_test pytest tests` (the test database is wiped).

Releases: add a new version at the top of [CHANGELOG.md](CHANGELOG.md). When it lands on `main`, GitHub Actions creates the tag and release, pushes the image `ghcr.io/siliconserver/aviationlog:<version>` and `:latest` (amd64 and arm64) and attaches the desktop apps for Windows, macOS and Debian to the release.

Desktop apps: `pip install -r requirements.txt -r desktop/requirements.txt && python desktop/build.py` builds the package for the system you run it on (Windows needs [Inno Setup 6](https://jrsoftware.org/isinfo.php); Linux needs `libgirepository1.0-dev libcairo2-dev gir1.2-webkit2-4.1` from apt). The PostgreSQL 16 that is bundled comes from the [pgserver](https://github.com/orm011/pgserver) package. To try the launcher without building: `python desktop/aviationlog_desktop.py`.

## License

MIT, see [LICENSE](LICENSE).
