import json
import os
import urllib.request

from flask import Blueprint, render_template

bp = Blueprint("download", __name__)

GITHUB_REPO = os.environ.get("GITHUB_REPO", "")


@bp.route("/win")
def download_win():
    if not GITHUB_REPO:
        return "Download not configured.", 503

    version = None
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "freeviewer-server"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            version = data.get("tag_name")
    except Exception:
        pass

    url = f"https://github.com/{GITHUB_REPO}/releases/latest/download/FreeViewer.zip"
    return render_template("download.html", download_url=url, version=version)
