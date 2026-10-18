# -*- coding: utf-8 -*-
"""Generate a CSV export preview from the live audit DB using the plugin's own code."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_audit as wa


class _Logger:
	def info(self, *a, **k): pass
	def warning(self, *a, **k): pass
	def error(self, *a, **k): pass
	def debug(self, *a, **k): pass


class _Server:
	logger = _Logger()
	data_folder = tempfile.mkdtemp(prefix="wh_export_")

	def get_data_folder(self):
		return self.data_folder

	def get_mcdr_config(self):
		return {}

	def get_server_information(self):
		class _Info:
			version = '1.20.4'
		return _Info()

	def load_config_simple(self, file_name, default_config=None, **kwargs):
		return dict(default_config or {})

	def save_config_simple(self, *a, **k):
		pass

	def is_rcon_running(self):
		return False

	def is_server_running(self):
		return False


def main():
	live_db = sys.argv[1]
	out_csv = sys.argv[2]
	audit = wa.WarehouseAudit(_Server())
	audit.load_config()
	audit.db_path = live_db
	audit.export_dir = os.path.dirname(out_csv)
	options = audit._parse_export_args("")
	count = audit._write_csv(options, out_csv)
	print("exported {} rows -> {}".format(count, out_csv))
	with open(out_csv, encoding="utf-8-sig") as f:
		lines = f.read().splitlines()
	print("---- CSV content ({} lines) ----".format(len(lines)))
	for line in lines:
		print(line)


if __name__ == "__main__":
	main()
