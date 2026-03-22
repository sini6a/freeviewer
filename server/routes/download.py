import os

from flask import Blueprint, redirect, render_template

bp = Blueprint("download", __name__)

GITHUB_REPO = os.environ.get("GITHUB_REPO", "")


@bp.route("/win")
def download_win():
    if not GITHUB_REPO:
        return "Download not configured.", 503
    url = f"https://github.com/{GITHUB_REPO}/releases/latest/download/FreeViewer.exe"
    return render_template("download.html", download_url=url)
