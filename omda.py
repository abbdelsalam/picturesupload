import io
import hmac
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st
from PIL import Image

import cloudinary
import cloudinary.uploader


# -----------------------------
# CONFIG
# -----------------------------
MAX_OTHER_IMAGES = 8
SKU_REGEX = r"^([^_]+)"
MAIN_MARKERS = ["main", "hero", "primary", "front"]

OVERWRITE_DUPLICATES = True
DEFAULT_CLOUDINARY_FOLDER = "amazon-images"

# Safety limits for Streamlit Cloud
MAX_ZIPS_PER_RUN = 10
MAX_TOTAL_IMAGES_PER_RUN = 250


# -----------------------------
# ACCESS CONTROL
# -----------------------------
def get_app_passwords() -> List[str]:
    passwords: List[str] = []

    single_password = str(st.secrets.get("APP_PASSWORD", "")).strip()
    if single_password:
        passwords.append(single_password)

    multiple_passwords = st.secrets.get("APP_PASSWORDS", [])
    if isinstance(multiple_passwords, str):
        passwords.extend(p.strip() for p in multiple_passwords.split(",") if p.strip())
    else:
        passwords.extend(str(p).strip() for p in multiple_passwords if str(p).strip())

    env_password = os.getenv("APP_PASSWORD", "").strip()
    if env_password:
        passwords.append(env_password)

    env_passwords = os.getenv("APP_PASSWORDS", "")
    if env_passwords:
        passwords.extend(p.strip() for p in env_passwords.split(",") if p.strip())

    return passwords


def require_app_password() -> None:
    passwords = get_app_passwords()

    if not passwords:
        st.error("Missing app password. Set APP_PASSWORD or APP_PASSWORDS in Streamlit secrets.")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("Amazon Image Host")

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        submitted_password = password.encode("utf-8")

        if any(
            hmac.compare_digest(submitted_password, saved_password.encode("utf-8"))
            for saved_password in passwords
        ):
            st.session_state["authenticated"] = True
            st.rerun()

        st.error("Invalid password.")

    st.stop()


# -----------------------------
# CLOUDINARY
# -----------------------------
def get_cloudinary_config() -> Tuple[str, str, str, str]:
    cloud_name = str(st.secrets.get("CLOUDINARY_CLOUD_NAME", "")).strip()
    api_key = str(st.secrets.get("CLOUDINARY_API_KEY", "")).strip()
    api_secret = str(st.secrets.get("CLOUDINARY_API_SECRET", "")).strip()
    folder = str(st.secrets.get("CLOUDINARY_FOLDER", DEFAULT_CLOUDINARY_FOLDER)).strip()

    if not cloud_name:
        cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    if not api_key:
        api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    if not api_secret:
        api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()

    env_folder = os.getenv("CLOUDINARY_FOLDER", "").strip()
    if env_folder:
        folder = env_folder

    if not (cloud_name and api_key and api_secret):
        raise RuntimeError(
            "Missing Cloudinary credentials.\n"
            "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET."
        )

    return cloud_name, api_key, api_secret, folder


def init_cloudinary() -> str:
    cloud_name, api_key, api_secret, folder = get_cloudinary_config()

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )

    return folder


@dataclass
class UploadedAsset:
    filename: str
    content: bytes
    forced_sku: Optional[str] = None


# -----------------------------
# HELPERS
# -----------------------------
def guess_sku(filename: str, sku_regex: str) -> str:
    base = os.path.basename(filename)
    m = re.match(sku_regex, base)
    return m.group(1) if m else ""


def get_sku(filename: str, forced_sku: Optional[str] = None) -> str:
    if forced_sku:
        return forced_sku

    sku = guess_sku(filename, SKU_REGEX)
    return sku if sku else "__UNMATCHED__"


def is_main_image(filename: str) -> bool:
    b = os.path.basename(filename).lower()
    return any(marker in b for marker in MAIN_MARKERS)


def sniff_image_bytes(content: bytes) -> Tuple[str, str]:
    try:
        im = Image.open(io.BytesIO(content))
        fmt = (im.format or "").upper()
        w, h = im.size
        im.close()
    except Exception:
        return "application/octet-stream", "Not a readable image."

    if fmt == "JPEG":
        mime = "image/jpeg"
    elif fmt == "PNG":
        mime = "image/png"
    else:
        mime = "application/octet-stream"

    warn = ""

    if max(w, h) < 1000:
        warn = f"Low resolution ({w}x{h}). Consider >=1000px for Amazon zoom."

    if mime == "application/octet-stream":
        warn = (warn + " " if warn else "") + f"Unsupported format ({fmt}). Use JPG/PNG."

    return mime, warn


def sanitize_public_id(sku: str, original_name: str) -> str:
    base = os.path.basename(original_name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)

    sku_clean = re.sub(r"[^A-Za-z0-9._-]+", "_", sku)
    base_no_ext = os.path.splitext(base)[0]

    return f"{sku_clean}__{base_no_ext}"


def cloudinary_upload_image(
    folder: str,
    sku: str,
    filename: str,
    content: bytes,
    mime_type: str,
    overwrite: bool = True,
) -> str:
    public_id = sanitize_public_id(sku, filename)

    if mime_type == "image/jpeg":
        fmt = "jpg"
    elif mime_type == "image/png":
        fmt = "png"
    else:
        fmt = None

    res = cloudinary.uploader.upload(
        content,
        resource_type="image",
        folder=folder,
        public_id=public_id,
        overwrite=overwrite,
        invalidate=True,
        unique_filename=False,
        use_filename=False,
        format=fmt,
    )

    return str(res["secure_url"])


def build_output_df(urls_by_sku: Dict[str, List[str]]) -> pd.DataFrame:
    rows = []

    for sku, urls in urls_by_sku.items():
        if sku == "__UNMATCHED__":
            continue

        main = urls[0] if urls else ""
        others = urls[1:1 + MAX_OTHER_IMAGES]

        row = {
            "SKU": sku,
            "MainImageURL": main,
        }

        for i in range(MAX_OTHER_IMAGES):
            row[f"OtherImageURL{i + 1}"] = others[i] if i < len(others) else ""

        rows.append(row)

    columns = ["SKU", "MainImageURL"] + [f"OtherImageURL{i + 1}" for i in range(MAX_OTHER_IMAGES)]

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(rows).sort_values("SKU")


def count_images_in_uploads(uploads) -> Tuple[int, int]:
    zip_count = 0
    image_count = 0

    for f in uploads:
        name = f.name.lower()

        if name.endswith(".zip"):
            zip_count += 1

            try:
                with zipfile.ZipFile(f, "r") as z:
                    for item in z.namelist():
                        item_lower = item.lower()

                        if item.endswith("/") or item.startswith("__MACOSX/"):
                            continue

                        if item_lower.endswith((".jpg", ".jpeg", ".png")):
                            image_count += 1
            except Exception:
                pass

            f.seek(0)

        elif name.endswith((".jpg", ".jpeg", ".png")):
            image_count += 1

    return zip_count, image_count


def collect_preview_rows(uploads) -> List[Dict[str, str]]:
    preview_rows = []

    for f in uploads:
        name = f.name

        if name.lower().endswith(".zip"):
            zip_sku = os.path.splitext(os.path.basename(name))[0]

            try:
                with zipfile.ZipFile(f, "r") as z:
                    for item in z.namelist():
                        item_lower = item.lower()

                        if item.endswith("/") or item.startswith("__MACOSX/"):
                            continue

                        if not item_lower.endswith((".jpg", ".jpeg", ".png")):
                            continue

                        preview_rows.append({
                            "SKU": zip_sku,
                            "Filename": item,
                            "Source": name,
                        })
            except Exception as e:
                preview_rows.append({
                    "SKU": "__ERROR__",
                    "Filename": name,
                    "Source": f"ZIP error: {e}",
                })

            f.seek(0)

        else:
            sku = get_sku(name)
            preview_rows.append({
                "SKU": sku,
                "Filename": name,
                "Source": "direct upload",
            })

    return preview_rows


def sort_group_images(items: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
    items = sorted(items, key=lambda x: os.path.basename(x[0]).lower())

    main_items = [x for x in items if is_main_image(x[0])]

    if main_items:
        main = main_items[0]
    else:
        main = items[0]

    others = [x for x in items if x is not main]

    return [main] + others


# -----------------------------
# STREAMLIT UI
# -----------------------------
st.set_page_config(page_title="Amazon Images → Cloudinary", layout="wide")

require_app_password()

st.title("Amazon Image Host — Cloudinary - Developed by absi developer @Tegaraty.com")

try:
    CLOUDINARY_FOLDER = init_cloudinary()
except Exception as e:
    st.error(str(e))
    st.stop()

st.subheader("1) Upload images or ZIP files")

uploads = st.file_uploader(
    "Drop JPG/PNG files or ZIP files",
    type=["jpg", "jpeg", "png", "zip"],
    accept_multiple_files=True,
)

if not uploads:
    st.stop()

zip_count, total_images = count_images_in_uploads(uploads)

if zip_count > MAX_ZIPS_PER_RUN:
    st.error(f"Too many ZIP files. Please upload maximum {MAX_ZIPS_PER_RUN} ZIP files per run.")
    st.stop()

if total_images > MAX_TOTAL_IMAGES_PER_RUN:
    st.error(
        f"Too many images in this run: {total_images}. "
        f"Please keep it under {MAX_TOTAL_IMAGES_PER_RUN} images."
    )
    st.stop()

st.info(f"ZIP files: {zip_count} • Total images detected: {total_images}")

preview_rows = collect_preview_rows(uploads)

st.subheader("2) Review detected SKUs")
st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, height=320)

st.subheader("3) Upload to Cloudinary + generate URLs")

go = st.button("Upload & Generate URLs", type="primary")

if go:
    progress = st.progress(0)
    status = st.empty()

    urls_by_sku: Dict[str, List[str]] = {}
    log_rows = []

    done = 0
    total = max(total_images, 1)

    for f in uploads:
        file_name = f.name

        # -----------------------------
        # Process ZIP one by one
        # -----------------------------
        if file_name.lower().endswith(".zip"):
            zip_sku = os.path.splitext(os.path.basename(file_name))[0]

            try:
                with zipfile.ZipFile(f, "r") as z:
                    zip_items = []

                    for item in z.namelist():
                        item_lower = item.lower()

                        if item.endswith("/") or item.startswith("__MACOSX/"):
                            continue

                        if not item_lower.endswith((".jpg", ".jpeg", ".png")):
                            continue

                        data = z.read(item)
                        zip_items.append((item, data))

                    ordered_items = sort_group_images(zip_items)

                    for image_name, data in ordered_items:
                        sku = zip_sku

                        mime, warn = sniff_image_bytes(data)

                        if mime == "application/octet-stream":
                            log_rows.append({
                                "SKU": sku,
                                "Filename": image_name,
                                "Status": "Skipped",
                                "Detail": warn or "Unsupported format",
                            })

                            done += 1
                            progress.progress(min(done / total, 1.0))
                            del data
                            continue

                        status.write(f"Uploading {image_name} → Cloudinary ({sku})")

                        try:
                            url = cloudinary_upload_image(
                                folder=CLOUDINARY_FOLDER,
                                sku=sku,
                                filename=image_name,
                                content=data,
                                mime_type=mime,
                                overwrite=OVERWRITE_DUPLICATES,
                            )

                            urls_by_sku.setdefault(sku, []).append(url)

                            log_rows.append({
                                "SKU": sku,
                                "Filename": image_name,
                                "Status": "Uploaded",
                                "Detail": url,
                            })

                        except Exception as e:
                            log_rows.append({
                                "SKU": sku,
                                "Filename": image_name,
                                "Status": "Failed",
                                "Detail": str(e),
                            })

                        done += 1
                        progress.progress(min(done / total, 1.0))

                        del data

                    del zip_items

            except Exception as e:
                log_rows.append({
                    "SKU": zip_sku,
                    "Filename": file_name,
                    "Status": "Failed",
                    "Detail": f"ZIP error: {e}",
                })

            f.seek(0)

        # -----------------------------
        # Process direct image upload
        # -----------------------------
        else:
            sku = get_sku(file_name)

            if sku == "__UNMATCHED__":
                log_rows.append({
                    "SKU": sku,
                    "Filename": file_name,
                    "Status": "Skipped",
                    "Detail": "Could not detect SKU",
                })
                continue

            data = f.read()
            mime, warn = sniff_image_bytes(data)

            if mime == "application/octet-stream":
                log_rows.append({
                    "SKU": sku,
                    "Filename": file_name,
                    "Status": "Skipped",
                    "Detail": warn or "Unsupported format",
                })

                done += 1
                progress.progress(min(done / total, 1.0))
                del data
                continue

            status.write(f"Uploading {file_name} → Cloudinary ({sku})")

            try:
                url = cloudinary_upload_image(
                    folder=CLOUDINARY_FOLDER,
                    sku=sku,
                    filename=file_name,
                    content=data,
                    mime_type=mime,
                    overwrite=OVERWRITE_DUPLICATES,
                )

                urls_by_sku.setdefault(sku, []).append(url)

                log_rows.append({
                    "SKU": sku,
                    "Filename": file_name,
                    "Status": "Uploaded",
                    "Detail": url,
                })

            except Exception as e:
                log_rows.append({
                    "SKU": sku,
                    "Filename": file_name,
                    "Status": "Failed",
                    "Detail": str(e),
                })

            done += 1
            progress.progress(min(done / total, 1.0))

            del data
            f.seek(0)

    status.empty()
    st.success("Finished.")

    out_df = build_output_df(urls_by_sku)

    st.subheader("Outputs")

    st.write("Amazon mapping CSV")
    st.dataframe(out_df, use_container_width=True, height=320)

    st.download_button(
        "Download mapping CSV",
        data=out_df.to_csv(index=False).encode("utf-8"),
        file_name="amazon_image_urls.csv",
        mime="text/csv",
    )

    log_df = pd.DataFrame(log_rows)

    st.write("Upload log")
    st.dataframe(log_df, use_container_width=True, height=240)

    st.download_button(
        "Download upload log CSV",
        data=log_df.to_csv(index=False).encode("utf-8"),
        file_name="upload_log.csv",
        mime="text/csv",
    )
