# ZECTRIX delivery contract

This project targets the official ZECTRIX cloud image API used by NOTE4.

- Device discovery: `GET /open/v1/devices` with `X-API-Key`.
- Image delivery: `POST /open/v1/devices/{deviceId}/display/image`.
- Multipart fields: `pageId`, `dither=false`, and one PNG file under `images`.
- Expected image: exactly 400 × 300 pixels, Pillow mode `1` after rendering.

`dither=false` is intentional because the renderer already produces a deterministic one-bit image. Device and API identifiers are never written into image output or persistent diagnostic state.

The endpoint base URL is configurable for testing or compatible services, but production use should retain HTTPS and the vendor endpoint. A live push is never part of tests or preview mode.
