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
# FIXED CONFIG (NO UI SETTINGS)
# -----------------------------
REPO_INPUT = "abbdelsalam/amazon-image-host"  # kept for compatibility; unused now
MAX_OTHER_IMAGES = 8

SKU_REGEX = r"^([^_]+)"  # anww_main.png, anww_1.png => SKU=anww

# If filename contains any of these, treat it as "main"
MAIN_MARKERS = ["main", "hero", "primary", "front"]

# Cloudinary behavior
OVERWRITE_DUPLICATES = True  # True = overwrite same public_id, False = keep existing
DEFAULT_CLOUDINARY_FOLDER = "amazon-images"


# -----------------------------
# SIMPLE APP ACCESS CONTROL
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
        if any(hmac.compare_digest(submitted_password, saved_password.encode("utf-8")) for saved_password in passwords):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Invalid password.")

    st.stop()


# -----------------------------
# CLOUDINARY CONFIG (NO UI)
# -----------------------------
def get_cloudinary_config() -> Tuple[str, str, str, str]:
    # Preferred: Streamlit secrets
    cloud_name = str(st.secrets.get("CLOUDINARY_CLOUD_NAME", "")).strip()
    api_key = str(st.secrets.get("CLOUDINARY_API_KEY", "")).strip()
    api_secret = str(st.secrets.get("CLOUDINARY_API_SECRET", "")).strip()
    folder = str(st.secrets.get("CLOUDINARY_FOLDER", DEFAULT_CLOUDINARY_FOLDER)).strip()

    # Fallback: environment variables
    if not cloud_name:
        cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    if not api_key:
        api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    if not api_secret:
        api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
    if not folder or folder == DEFAULT_CLOUDINARY_FOLDER:
        folder = os.getenv("CLOUDINARY_FOLDER", folder).strip() or DEFAULT_CLOUDINARY_FOLDER

    if not (cloud_name and api_key and api_secret):
        raise RuntimeError(
            "Missing Cloudinary credentials.\n"
            "Set in Streamlit secrets (.streamlit/secrets.toml):\n"
            "  CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET (optional CLOUDINARY_FOLDER)\n"
            "or set env vars with the same names."
        )
    return cloud_name, api_key, api_secret, folder


def init_cloudinary() -> str:
    cloud_name, api_key, api_secret, folder = get_cloudinary_config()
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True
    )
    return folder


@dataclass
class UploadedAsset:
    filename: str
    content: bytes
    forced_sku: Optional[str] = None  # ✅ if set, overrides regex-based SKU


# -----------------------------
# HELPERS: SKU + validation
# -----------------------------
def guess_sku(filename: str, sku_regex: str) -> str:
    base = os.path.basename(filename)
    m = re.match(sku_regex, base)
    return m.group(1) if m else ""


def is_main_image(filename: str) -> bool:
    b = os.path.basename(filename).lower()
    return any(m in b for m in MAIN_MARKERS)


def sniff_image(asset: UploadedAsset) -> Tuple[str, str]:
    """
    Returns (mime_type, warning). warning empty if OK-ish.
    """
    try:
        im = Image.open(io.BytesIO(asset.content))
        fmt = (im.format or "").upper()
        w, h = im.size
    except Exception:
        return "application/octet-stream", "Not a readable image."

    if fmt == "JPEG":
        mime = "image/jpeg"
    elif fmt == "PNG":
        mime = "image/png"
    else:
        mime = "application/octet-stream"

    warn = ""
    # rule-of-thumb for Amazon zoom: 1000px+ on longest side
    if max(w, h) < 1000:
        warn = f"Low resolution ({w}x{h}). Consider >=1000px for zoom."
    if mime == "application/octet-stream":
        warn = (warn + " " if warn else "") + f"Unsupported format ({fmt}). Use JPG/PNG."
    return mime, warn


def expand_zip(zip_asset: UploadedAsset) -> List[UploadedAsset]:
    out = []
    zip_sku = os.path.splitext(os.path.basename(zip_asset.filename))[0]  # ✅ zip name as SKU

    with zipfile.ZipFile(io.BytesIO(zip_asset.content), "r") as z:
        for name in z.namelist():
            if name.endswith("/") or name.startswith("__MACOSX/"):
                continue
            data = z.read(name)
            out.append(
                UploadedAsset(
                    filename=name,
                    content=data,
                    forced_sku=zip_sku  # ✅ force grouping
                )
            )
    return out

def build_mapping(assets: List[UploadedAsset], sku_regex: str) -> Dict[str, List[UploadedAsset]]:
    mapping: Dict[str, List[UploadedAsset]] = {}
    for a in assets:
        if a.forced_sku:  # ✅ ZIP rule wins
            sku = a.forced_sku
        else:
            sku = guess_sku(a.filename, sku_regex)

        if not sku:
            sku = "__UNMATCHED__"

        mapping.setdefault(sku, []).append(a)
    return mapping


def order_images(images: List[UploadedAsset]) -> Tuple[UploadedAsset, List[UploadedAsset]]:
    sorted_imgs = sorted(images, key=lambda x: os.path.basename(x.filename).lower())
    mains = [x for x in sorted_imgs if is_main_image(x.filename)]
    main = mains[0] if mains else sorted_imgs[0]
    others = [x for x in sorted_imgs if x is not main]
    return main, others


def sanitize_public_id(sku: str, original_name: str) -> str:
    """
    Cloudinary public_id should be stable and safe.
    We store inside a folder, so public_id excludes folder prefix.
    """
    base = os.path.basename(original_name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    sku_clean = re.sub(r"[^A-Za-z0-9._-]+", "_", sku)
    base_no_ext = os.path.splitext(base)[0]
    return f"{sku_clean}__{base_no_ext}"


def cloudinary_upload_image(
    folder: str,
    sku: str,
    img: UploadedAsset,
    mime_type: str,
    overwrite: bool = True
) -> str:
    """
    Upload bytes -> returns secure_url
    """
    public_id = sanitize_public_id(sku, img.filename)

    # Infer format for better consistency
    fmt = "jpg" if mime_type == "image/jpeg" else "png" if mime_type == "image/png" else None

    res = cloudinary.uploader.upload(
        img.content,
        resource_type="image",
        folder=folder,
        public_id=public_id,
        overwrite=overwrite,
        invalidate=True,          # refresh CDN cache if overwriting
        unique_filename=False,    # keep public_id stable
        use_filename=False,
        format=fmt                # optional: force output format
    )
    return str(res["secure_url"])


def build_output_df(urls_by_sku: Dict[str, List[str]]) -> pd.DataFrame:
    rows = []
    for sku, urls in urls_by_sku.items():
        if sku == "__UNMATCHED__":
            continue
        main = urls[0] if urls else ""
        others = urls[1:1 + MAX_OTHER_IMAGES]
        row = {"SKU": sku, "MainImageURL": main}
        for i in range(MAX_OTHER_IMAGES):
            row[f"OtherImageURL{i+1}"] = others[i] if i < len(others) else ""
        rows.append(row)

    if not rows:
        return pd.DataFrame(
            columns=["SKU", "MainImageURL"] + [f"OtherImageURL{i+1}" for i in range(MAX_OTHER_IMAGES)]
        )
    return pd.DataFrame(rows).sort_values("SKU")


# -----------------------------
# STREAMLIT UI (UPLOAD ONLY)
# -----------------------------
st.set_page_config(page_title="Amazon Images → Cloudinary", layout="wide")
require_app_password()
st.title("Amazon Image Host — Cloudinary - Developed by absi developer @Tegaraty.com")

# Preflight: Cloudinary credentials
try:
    CLOUDINARY_FOLDER = init_cloudinary()
except Exception as e:
    st.error(str(e))
    st.stop()

st.subheader("1) Upload images (or a ZIP)")
uploads = st.file_uploader(
    "Drop JPG/PNG files or a ZIP",
    type=["jpg", "jpeg", "png", "zip"],
    accept_multiple_files=True
)

if not uploads:
    st.stop()

# Build assets list, expand zip(s)
assets: List[UploadedAsset] = []
for f in uploads:
    data = f.read()
    if f.name.lower().endswith(".zip"):
        assets.extend(expand_zip(UploadedAsset(filename=f.name, content=data)))
    else:
        assets.append(UploadedAsset(filename=f.name, content=data))

mapping = build_mapping(assets, SKU_REGEX)

# Preview table
preview_rows = []
warn_count = 0
for sku, imgs in mapping.items():
    for img in imgs:
        mime, warn = sniff_image(img)
        if warn:
            warn_count += 1
        preview_rows.append({"SKU": sku, "Filename": img.filename, "MIME": mime, "Warning": warn})

st.subheader("2) Review detected SKUs")
st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, height=300)
st.info(f"SKU groups: {len(mapping)} • Warnings: {warn_count}")
if "__UNMATCHED__" in mapping:
    st.warning("Some files did not match your SKU rule and were grouped as __UNMATCHED__.")

st.subheader("3) Upload to Cloudinary + generate URLs")
go = st.button("Upload & Generate URLs", type="primary")

if go:
    progress = st.progress(0)
    status = st.empty()

    urls_by_sku: Dict[str, List[str]] = {}
    log_rows = []

    # Total number of images we intend to process (excluding unsupported)
    flat_list = []
    for sku, imgs in mapping.items():
        if sku == "__UNMATCHED__":
            continue
        main, others = order_images(imgs)
        flat_list.extend([(sku, x) for x in [main] + others])

    total = max(len(flat_list), 1)
    done = 0

    for sku, imgs in mapping.items():
        if sku == "__UNMATCHED__":
            continue

        main, others = order_images(imgs)
        ordered = [main] + others
        sku_urls: List[str] = []

        for img in ordered:
            mime, warn = sniff_image(img)
            if mime == "application/octet-stream":
                log_rows.append({"SKU": sku, "Filename": img.filename, "Status": "Skipped", "Detail": "Unsupported format"})
                done += 1
                progress.progress(min(done / total, 1.0))
                continue

            status.write(f"Uploading {img.filename} → Cloudinary ({sku})")

            try:
                url = cloudinary_upload_image(
                    folder=CLOUDINARY_FOLDER,
                    sku=sku,
                    img=img,
                    mime_type=mime,
                    overwrite=OVERWRITE_DUPLICATES
                )
                sku_urls.append(url)
                log_rows.append({"SKU": sku, "Filename": img.filename, "Status": "Uploaded", "Detail": url})
            except Exception as e:
                log_rows.append({"SKU": sku, "Filename": img.filename, "Status": "Failed", "Detail": str(e)})

            done += 1
            progress.progress(min(done / total, 1.0))

        urls_by_sku[sku] = sku_urls

    st.success("Finished.")

    out_df = build_output_df(urls_by_sku)
    st.subheader("Outputs")

    st.write("**Amazon mapping CSV** (MainImageURL + OtherImageURL1..8)")
    st.dataframe(out_df, use_container_width=True, height=320)

    st.download_button(
        "Download mapping CSV",
        data=out_df.to_csv(index=False).encode("utf-8"),
        file_name="amazon_image_urls.csv",
        mime="text/csv"
    )

    log_df = pd.DataFrame(log_rows)
    st.write("**Upload log**")
    st.dataframe(log_df, use_container_width=True, height=240)

    st.download_button(
        "Download upload log CSV",
        data=log_df.to_csv(index=False).encode("utf-8"),
        file_name="upload_log.csv",
        mime="text/csv"
    )
