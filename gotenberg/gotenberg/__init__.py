# Copyright (c) 2025, AgriTheory and contributors
# For license information, please see license.txt

from contextlib import contextmanager
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse
import requests

from gotenberg_client import GotenbergClient
from bs4 import BeautifulSoup

import frappe
from frappe.utils.print_format import validate_print_permission
from frappe.translate import print_language
from frappe.utils import get_bench_path, get_site_path


@contextmanager
def get_temp_file_content(url: str) -> tuple[str | None, str | None]:
	try:
		file_url = unquote(url)
		parsed_url = urlparse(file_url)

		# Check if URL is remote
		is_remote = bool(parsed_url.scheme and parsed_url.netloc)

		if is_remote:
			try:
				response = requests.get(file_url)
				response.raise_for_status()
				content = response.content
				filename = Path(parsed_url.path).name or "remote_file"
			except Exception as e:
				frappe.log_error(f"Error downloading remote file {file_url}: {str(e)}")
				yield None, None
				return
		else:
			filename = Path(file_url).name
			content = _get_file_content(file_url)

		if not content:
			yield None, None
			return

		with tempfile.NamedTemporaryFile(delete=False) as temp_file:
			temp_file.write(content)
			yield temp_file.name, filename

	except Exception as e:
		frappe.log_error(f"Error handling file {url}: {str(e)}")
		yield None, None


def _get_file_content(file_url: str) -> bytes | None:
	try:
		# Handle remote URLs
		parsed_url = urlparse(file_url)
		if parsed_url.scheme and parsed_url.netloc:
			try:
				response = requests.get(file_url)
				response.raise_for_status()
				return response.content
			except Exception as e:
				frappe.log_error(f"Error downloading remote file {file_url}: {str(e)}")
				return file_url

		# Handle /files/ and /private/files/ URLs
		if file_url.startswith(("/files/", "/private/files/")):
			file_doc = frappe.get_doc("File", {"file_url": file_url})
			if file_doc:
				content = file_doc.get_content()
				if content:
					frappe.log_error(f"Got {len(content)} content")
					return content

		if file_url.startswith("/assets/"):
			asset_path = Path(get_bench_path()) / "sites" / "assets" / file_url.lstrip("/assets/")
			if asset_path.exists():
				return asset_path.read_bytes()

		public_path = (
			Path(get_bench_path()) / "sites" / get_site_path()[1:] / "public/files" / file_url
		).resolve()

		if public_path.exists():
			return public_path.read_bytes()

		frappe.log_error(f"File not found in any location: {file_url}")
		return file_url

	except Exception as e:
		frappe.log_error(f"Error retrieving file content: {str(e)}")
		return file_url


def collect_resources_and_modify_html(html_content):
	soup = BeautifulSoup(html_content, "html.parser")
	resources = []

	# Remove all elements with print-hide class
	for el in soup.select(".print-hide"):
		el.decompose()

	# Process image sources
	for img in soup.find_all("img"):
		src = img.get("src")
		frappe.log_error(f"Processing image: {Path(src).name}")
		if src and not src.startswith("data:"):
			content = _get_file_content(src)

			if isinstance(content, bytes):
				with tempfile.NamedTemporaryFile(delete=False, suffix=Path(src).suffix) as temp_file:
					temp_file.write(content)
					temp_name = temp_file.name
					resources.append((temp_name, Path(src).name))
					img["src"] = Path(src).name
					frappe.log_error(f"Created temp: {Path(temp_name).name}")
			else:
				img["src"] = content

	# Process CSS files
	for link in soup.find_all("link", rel="stylesheet"):
		href = link.get("href")
		if href:
			content = _get_file_content(href)
			if isinstance(content, bytes):
				with tempfile.NamedTemporaryFile(delete=False, suffix=".css") as temp_file:
					temp_file.write(content)
					resources.append((temp_file.name, Path(href).name))
					link["href"] = Path(href).name
			else:
				link["href"] = content

	frappe.log_error(f"Found {len(resources)} resources")
	return str(soup), resources


@frappe.whitelist(allow_guest=True)
def download_pdf(
	doctype: str, name: str, format=None, doc=None, no_letterhead=0, language=None, letterhead=None
):
	doc = doc or frappe.get_doc(doctype, name)
	validate_print_permission(doc)

	GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://localhost:3001")
	gotenberg_client = GotenbergClient(GOTENBERG_URL)

	with print_language(language):
		html_content = frappe.get_print(
			doctype, name, format, doc=doc, letterhead=letterhead, no_letterhead=no_letterhead, as_pdf=False
		)

		temp_files = []
		try:
			clean_html, resources = collect_resources_and_modify_html(html_content)
			temp_files.extend(temp_path for temp_path, _ in resources)

			with gotenberg_client.chromium.html_to_pdf() as route:
				route.string_index(clean_html)

				# Debug log resources before sending
				frappe.log_error("HTML Resource references:", clean_html)
				for img in BeautifulSoup(clean_html, "html.parser").find_all("img"):
					frappe.log_error(f"Image src in HTML: {img.get('src')}")

				# Log resource files being sent
				if resources:
					for temp_path, filename in resources:
						frappe.log_error(f"Resource mapping: {temp_path} -> {filename}")
						with open(temp_path, "rb") as f:
							content = f.read()
							frappe.log_error(f"Resource size: {filename} = {len(content)} bytes")
						route.resource(resource=Path(temp_path), name=filename)	

				response = route.run()
				pdf_content = response.content

		except Exception as e:
			frappe.log_error(f"PDF error: {str(e)}")
			frappe.throw(frappe._("PDF generation failed: {0}").format(str(e)))
		finally:
			# Clean up temporary files
			for temp_path in temp_files:
				try:
					if os.path.exists(temp_path):
						os.unlink(temp_path)
				except Exception:
					frappe.log_error(f"Failed to delete: {temp_path}")

	frappe.local.response.filename = "{name}.pdf".format(
		name=name.replace(" ", "-").replace("/", "-")
	)
	frappe.local.response.filecontent = pdf_content
	frappe.local.response.type = "pdf"
