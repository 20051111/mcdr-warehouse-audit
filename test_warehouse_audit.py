# -*- coding: utf-8 -*-
"""WarehouseAudit 离线单元测试 (不连接真实服务器)。

覆盖: SNBT 解析 / 区域文件解析 / RCON 响应解析 / diff 逻辑 / 玩家归因 /
CSV 导出 / 指令参数解析 / 完整轮询周期 (模拟 rcon_query)。
"""
import csv
import copy
import datetime
import logging
import os
import queue
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import warehouse_audit as wa  # noqa: E402


# ---------------------------------------------------------------------------
# 模拟 MCDR PluginServerInterface
# ---------------------------------------------------------------------------

class FakeLogger:
	def __init__(self):
		self.messages = []

	def _log(self, level, msg, *args):
		self.messages.append((level, str(msg) % args if args else str(msg)))

	def info(self, msg, *args):
		self._log('info', msg, *args)

	def warning(self, msg, *args):
		self._log('warning', msg, *args)

	def error(self, msg, *args):
		self._log('error', msg, *args)

	def debug(self, msg, *args):
		self._log('debug', msg, *args)

	def exception(self, msg, *args):
		self._log('exception', msg, *args)


class FakeSource:
	def __init__(self):
		self.replies = []

	def reply(self, message, **kwargs):
		self.replies.append(str(message))

	def has_permission(self, level):
		return True


class FakeServer:
	def __init__(self, data_folder):
		self.logger = FakeLogger()
		self._data_folder = data_folder
		self.rcon_responses = {}          # command -> response
		self.rcon_log = []
		self.registered_commands = []
		self.help_messages = []
		self._running = True
		self._rcon = True

	# --- MCDR API 子集 ---
	def get_data_folder(self):
		os.makedirs(self._data_folder, exist_ok=True)
		return self._data_folder

	def get_self_metadata(self):
		class M:
			version = '1.0.0'
		return M()

	def get_mcdr_config(self):
		return {'working_directory': 'server', 'plugin_directories': ['plugins']}

	def get_server_information(self):
		class _Info:
			version = '1.20.4'
		return _Info()

	def load_config_simple(self, file_name, default_config=None, **kwargs):
		return copy.deepcopy(default_config or {})

	def save_config_simple(self, config, file_name=None, **kwargs):
		pass

	def register_command(self, root_node):
		self.registered_commands.append(root_node)

	def register_help_message(self, prefix, message, permission=0):
		self.help_messages.append((prefix, message))

	def is_server_running(self):
		return self._running

	def is_rcon_running(self):
		return self._rcon

	def rcon_query(self, command):
		self.rcon_log.append(command)
		return self.rcon_responses.get(command)


def make_audit(tmpdir, responses=None, config_overrides=None, cleanup=None):
	server = FakeServer(os.path.join(tmpdir, 'config', 'warehouse_audit'))
	if responses:
		server.rcon_responses.update(responses)
	audit = wa.WarehouseAudit(server)
	audit.load_config()
	if config_overrides:
		audit.config.update(config_overrides)
	audit.db_path = os.path.join(tmpdir, 'test.db')
	audit.export_dir = os.path.join(tmpdir, 'export')
	if cleanup is not None:
		cleanup(audit.stop)
	return audit, server


def block_data_response(pos, payload):
	return '{}, {}, {} has the following block data: {}'.format(pos[0], pos[1], pos[2], payload)


# ---------------------------------------------------------------------------
# 1. SNBT 解析器
# ---------------------------------------------------------------------------

class TestSnbt(unittest.TestCase):
	def test_items_list(self):
		text = '[{Slot: 0b, id: "minecraft:diamond", Count: 5b}, {Slot: 1b, id: "minecraft:oak_planks", Count: 64b}]'
		value = wa.parse_snbt(text)
		self.assertEqual(value, [
			{'Slot': 0, 'id': 'minecraft:diamond', 'Count': 5},
			{'Slot': 1, 'id': 'minecraft:oak_planks', 'Count': 64},
		])

	def test_full_block_data(self):
		text = '{z: 9, x: 4, id: "minecraft:chest", y: 100, Items: []}'
		value = wa.parse_snbt(text)
		self.assertEqual(value['id'], 'minecraft:chest')
		self.assertEqual(value['Items'], [])

	def test_empty_containers(self):
		self.assertEqual(wa.parse_snbt('[]'), [])
		self.assertEqual(wa.parse_snbt('{}'), {})

	def test_quoted_string(self):
		self.assertEqual(wa.parse_snbt('"minecraft:chest"'), 'minecraft:chest')
		self.assertEqual(wa.parse_snbt("'hello world'"), 'hello world')

	def test_nested_enchant(self):
		text = ('[{Slot: 0b, id: "minecraft:diamond_sword", Count: 1b, '
				'tag: {Enchantments: [{id: "minecraft:sharpness", lvl: 3s}], Damage: 12}}]')
		value = wa.parse_snbt(text)
		self.assertEqual(value[0]['tag']['Enchantments'][0]['lvl'], 3)
		self.assertEqual(value[0]['tag']['Damage'], 12)

	def test_numbers(self):
		self.assertEqual(wa.parse_snbt('3b'), 3)
		self.assertEqual(wa.parse_snbt('-5'), -5)
		self.assertEqual(wa.parse_snbt('1.5f'), 1.5)
		self.assertAlmostEqual(wa.parse_snbt('-10146.425246841434d'), -10146.425246841434)
		self.assertEqual(wa.parse_snbt('100L'), 100)

	def test_pos_list(self):
		value = wa.parse_snbt('[-10146.425246841434d, -63.875d, 2739.505970373987d]')
		self.assertEqual(len(value), 3)
		self.assertAlmostEqual(value[0], -10146.425246841434)

	def test_string_with_escapes(self):
		self.assertEqual(wa.parse_snbt('"say \\"hi\\""'), 'say "hi"')

	def test_errors(self):
		for bad in ['', '{', '[1,', '{a:', '{"a" 1}', '[1 2]', '"unterminated']:
			with self.assertRaises(wa.SnbtError, msg=bad):
				wa.parse_snbt(bad)

	def test_whitespace_tolerance(self):
		value = wa.parse_snbt('  [ { Slot : 0b , id : "x" , Count : 2b } ]  ')
		self.assertEqual(value, [{'Slot': 0, 'id': 'x', 'Count': 2}])


# ---------------------------------------------------------------------------
# 2. RCON 响应解析
# ---------------------------------------------------------------------------

class TestResponseParsing(unittest.TestCase):
	def test_items_ok(self):
		resp = block_data_response((4, 100, 9), '[{Slot: 0b, id: "minecraft:diamond", Count: 5b}]')
		status, items = wa.parse_block_items_response(resp, (4, 100, 9))
		self.assertEqual(status, wa.READ_OK)
		self.assertEqual(items, {0: ('minecraft:diamond', 5, None)})

	def test_items_empty(self):
		status, items = wa.parse_block_items_response(
			block_data_response((4, 100, 9), '[]'), (4, 100, 9))
		self.assertEqual(status, wa.READ_OK)
		self.assertEqual(items, {})

	def test_absent(self):
		status, items = wa.parse_block_items_response(
			'The target block is not a block entity', (4, 100, 9))
		self.assertEqual(status, wa.READ_ABSENT)
		self.assertEqual(items, {})

	def test_shulker_without_items_tag(self):
		# 空潜影盒没有 Items 标签: 'Found no elements matching Items' -> 空容器, 必须可监控
		status, items = wa.parse_block_items_response(
			'Found no elements matching Items', (-2, 100, -1))
		self.assertEqual(status, wa.READ_OK)
		self.assertEqual(items, {})

	def test_skip_not_loaded(self):
		status, items = wa.parse_block_items_response(
			'That position is not loaded', (4, 100, 9))
		self.assertEqual(status, wa.READ_SKIP)
		self.assertIsNone(items)

	def test_skip_none_and_garbage(self):
		self.assertEqual(wa.parse_block_items_response(None, (0, 0, 0))[0], wa.READ_SKIP)
		self.assertEqual(wa.parse_block_items_response('', (0, 0, 0))[0], wa.READ_SKIP)
		self.assertEqual(wa.parse_block_items_response('weird error', (0, 0, 0))[0], wa.READ_SKIP)

	def test_skip_coord_mismatch(self):
		resp = block_data_response((1, 2, 3), '[]')
		self.assertEqual(wa.parse_block_items_response(resp, (4, 5, 6))[0], wa.READ_SKIP)

	def test_player_positions_concatenated(self):
		resp = ('bot_mob has the following entity data: [-10146.425246841434d, -63.875d, 2739.505970373987d]'
				'bot_loader has the following entity data: [-1266.9698091830412d, 256.0d, 343.2965851588893d]')
		players = wa.parse_player_positions(resp)
		self.assertEqual(set(players), {'bot_mob', 'bot_loader'})
		self.assertAlmostEqual(players['bot_mob'][0], -10146.425246841434)
		self.assertAlmostEqual(players['bot_loader'][2], 343.2965851588893)

	def test_player_dimensions(self):
		resp = ('bot_mob has the following entity data: "minecraft:overworld"'
				'bot_loader has the following entity data: "minecraft:the_nether"')
		dims = wa.parse_player_dimensions(resp)
		self.assertEqual(dims, {'bot_mob': 'minecraft:overworld', 'bot_loader': 'minecraft:the_nether'})

	def test_empty_player_response(self):
		self.assertEqual(wa.parse_player_positions(None), {})
		self.assertEqual(wa.parse_player_positions(''), {})


class TestItemContents(unittest.TestCase):
	"""取出盒子时读取其 NBT 内容物 (嵌套潜影盒/箱子)。"""

	NESTED = ('[{Slot: 1b, id: "minecraft:chest", Count: 1b, tag: {display: {Lore: [\'"(+NBT)"\']}, '
			  'BlockEntityTag: {id: "minecraft:chest", Items: [{Slot: 0b, id: "minecraft:packed_ice", Count: 64b}, '
			  '{Slot: 1b, id: "minecraft:packed_ice", Count: 64b}, {Slot: 2b, id: "minecraft:white_stained_glass", Count: 64b}, '
			  '{Slot: 3b, id: "minecraft:shulker_box", Count: 1b, tag: {BlockEntityTag: {id: "minecraft:shulker_box", '
			  'Items: [{Slot: 0b, id: "minecraft:observer", Count: 64b}]}}}]}}}]')

	def test_extract_nested_contents(self):
		text = '5, 100, 9 has the following block data: ' + self.NESTED
		status, items = wa.parse_block_items_response(text, (5, 100, 9))
		self.assertEqual(status, wa.READ_OK)
		entry = items[1]
		self.assertEqual(entry[0], 'minecraft:chest')
		self.assertEqual(entry[1], 1)
		self.assertIn('packed_ice x128', entry[2])     # 聚合
		self.assertIn('white_stained_glass x64', entry[2])
		self.assertIn('shulker_box x1', entry[2])
		# 只展开一层: 嵌套潜影盒里的 observer 不出现
		self.assertNotIn('observer', entry[2])

	def test_plain_item_no_contents(self):
		text = '5, 100, 9 has the following block data: [{Slot: 0b, id: "minecraft:diamond", Count: 5b}]'
		status, items = wa.parse_block_items_response(text, (5, 100, 9))
		self.assertIsNone(items[0][2])

	def test_empty_box_no_contents(self):
		text = ('5, 100, 9 has the following block data: [{Slot: 0b, id: "minecraft:shulker_box", Count: 1b, '
				'tag: {BlockEntityTag: {id: "minecraft:shulker_box", Items: []}}}]')
		status, items = wa.parse_block_items_response(text, (5, 100, 9))
		self.assertIsNone(items[0][2])

	def test_1205_component_format(self):
		text = ('5, 100, 9 has the following block data: [{Slot: 0b, id: "minecraft:shulker_box", count: 1b, '
				'components: {"minecraft:container": [{slot: 0, item: "minecraft:stone", count: 3}, '
				'{slot: 1, item: "minecraft:dirt", count: 2}]}}]')
		status, items = wa.parse_block_items_response(text, (5, 100, 9))
		self.assertEqual(items[0][2], 'minecraft:stone x3; minecraft:dirt x2')

	def test_contents_in_record(self):
		"""取出带内容物的盒子时, 记录携带内容概要。"""
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			old = {1: ('minecraft:chest', 1, 'packed_ice x128; white_stained_glass x64')}
			new = {}
			records = audit._diff_container(
				(5, 100, 9), 'minecraft:chest', '主仓库', 'minecraft:overworld',
				old, new, 't', 0.0, None, None, False)
			self.assertEqual(len(records), 1)
			self.assertEqual(records[0][16], 'packed_ice x128; white_stained_glass x64')

	def test_contents_long_truncated(self):
		items = [{'Slot': i, 'id': 'minecraft:item_{}'.format(i), 'Count': 64} for i in range(40)]
		entry = {'id': 'minecraft:chest', 'Count': 1,
				 'tag': {'BlockEntityTag': {'id': 'minecraft:chest', 'Items': items}}}
		summary = wa.extract_item_contents(entry)
		self.assertIsNotNone(summary)
		self.assertLessEqual(len(summary), 500)


# ---------------------------------------------------------------------------
# 3. diff 逻辑
# ---------------------------------------------------------------------------

class TestDiff(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)

	def tearDown(self):
		self.tmp.cleanup()

	def _diff(self, old, new, record_deposits=True):
		return self.audit._diff_container(
			(4, 100, 9), 'minecraft:chest', '主仓库', 'minecraft:overworld',
			old, new, '2026-10-18T15:00:00.000+08:00', 1760754000.0, 'Steve', 1.5, False,
			record_deposits)

	def test_take_items(self):
		old = {0: ('minecraft:diamond', 5)}
		new = {}
		records = self._diff(old, new)
		self.assertEqual(len(records), 1)
		r = records[0]
		self.assertEqual(r[9], 'minecraft:diamond')   # item_id
		self.assertEqual(r[10], -5)                   # delta
		self.assertEqual(r[11], 5)                    # before
		self.assertEqual(r[12], 0)                    # after
		self.assertEqual(r[13], 'Steve')
		self.assertEqual(r[8], -1, 'slot must not be recorded (legacy column = -1)')

	def test_put_items(self):
		records = self._diff({}, {2: ('minecraft:gold_ingot', 3)})
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0][10], 3)
		self.assertEqual(records[0][11], 0)
		self.assertEqual(records[0][12], 3)

	def test_put_items_not_recorded_when_deposits_off(self):
		# record_deposits=False (默认): 存入不记录
		records = self._diff({}, {2: ('minecraft:gold_ingot', 3)}, record_deposits=False)
		self.assertEqual(records, [])

	def test_take_still_recorded_when_deposits_off(self):
		records = self._diff({0: ('minecraft:diamond', 5)}, {0: ('minecraft:diamond', 2)},
							 record_deposits=False)
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0][10], -3)

	def test_partial_take(self):
		records = self._diff({0: ('minecraft:diamond', 5)}, {0: ('minecraft:diamond', 2)})
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0][10], -3)

	def test_item_replaced(self):
		records = self._diff({0: ('minecraft:diamond', 5)}, {0: ('minecraft:gold_ingot', 1)})
		self.assertEqual(len(records), 2)
		deltas = sorted(r[10] for r in records)
		self.assertEqual(deltas, [-5, 1])

	def test_item_replaced_deposits_off(self):
		# 替换时只记录旧物品取出, 新物品存入不记录
		records = self._diff({0: ('minecraft:diamond', 5)}, {0: ('minecraft:gold_ingot', 1)},
							 record_deposits=False)
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0][10], -5)
		self.assertEqual(records[0][9], 'minecraft:diamond')

	def test_no_change(self):
		self.assertEqual(self._diff({0: ('a', 1)}, {0: ('a', 1)}), [])

	def test_no_player(self):
		records = self.audit._diff_container(
			(0, 0, 0), 'minecraft:barrel', '', 'minecraft:overworld',
			{0: ('minecraft:dirt', 1)}, {}, 't', 0.0, None, None, False)
		self.assertIsNone(records[0][13])  # player
		self.assertIsNone(records[0][14])  # distance

	def test_ambiguous_flag(self):
		records = self.audit._diff_container(
			(0, 0, 0), 'minecraft:chest', '', 'minecraft:overworld',
			{}, {0: ('x', 1)}, 't', 0.0, 'A', 2.0, True)
		self.assertEqual(records[0][15], 1)


# ---------------------------------------------------------------------------
# 4. 玩家归因
# ---------------------------------------------------------------------------

class TestAttribution(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)

	def tearDown(self):
		self.tmp.cleanup()

	def test_nearest_player(self):
		players = {
			'far': ('minecraft:overworld', 20.0, 100.0, 10.0),
			'near': ('minecraft:overworld', 5.0, 100.0, 10.0),
		}
		name, dist, amb = self.audit._attribute_player((4, 100, 9), players, 'minecraft:overworld')
		self.assertEqual(name, 'near')
		self.assertTrue(0 < dist < 2)

	def test_cross_dimension_ignored(self):
		players = {'nether_guy': ('minecraft:the_nether', 4.5, 100.5, 9.5)}
		name, dist, amb = self.audit._attribute_player((4, 100, 9), players, 'minecraft:overworld')
		self.assertIsNone(name)
		self.assertIsNone(dist)

	def test_out_of_radius(self):
		players = {'far': ('minecraft:overworld', 100.0, 100.0, 100.0)}
		name, _, _ = self.audit._attribute_player((4, 100, 9), players, 'minecraft:overworld')
		self.assertIsNone(name)

	def test_ambiguous(self):
		players = {
			'a': ('minecraft:overworld', 4.5, 100.0, 10.5),
			'b': ('minecraft:overworld', 5.5, 100.0, 10.5),
		}
		name, _, amb = self.audit._attribute_player((4, 100, 9), players, 'minecraft:overworld')
		self.assertIsNotNone(name)
		self.assertTrue(amb)

	def test_unknown_dimension_player_counts(self):
		# dim 为 None (维度查询失败) 时不过滤维度
		players = {'x': (None, 4.5, 100.5, 9.5)}
		name, _, _ = self.audit._attribute_player((4, 100, 9), players, 'minecraft:overworld')
		self.assertEqual(name, 'x')


# ---------------------------------------------------------------------------
# 5. 区域文件解析 (使用镜像服真实区域文件, 只读)
# ---------------------------------------------------------------------------

class TestRegionFile(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.world = cls._find_mirror_world()
		if cls.world is None:
			raise unittest.SkipTest('mirror server world not found on this machine')

	@staticmethod
	def _find_mirror_world():
		"""在多个 1.20.4 服务器中找到镜像服 (其 r.0.0.mca 含仓库箱子)。"""
		import glob
		pattern = os.path.join('D:', os.sep, 'MC', '1.20', '1.20.1BC*', '1.20.4*', 'server', 'world')
		for cand in sorted(glob.glob(pattern)):
			path = os.path.join(cand, 'region', 'r.0.0.mca')
			if not os.path.isfile(path):
				continue
			# 该区域文件是否包含仓库范围内的箱子
			for chunk_x, chunk_z, root in wa.iter_region_file_chunks(path):
				for be in root.get('block_entities') or []:
					if be.get('id') == 'minecraft:chest':
						x, y, z = be.get('x'), be.get('y'), be.get('z')
						if -10 <= x <= 10 and 99 <= y <= 101 and -10 <= z <= 10:
							return cand
		return None

	def test_iter_chunks(self):
		path = os.path.join(self.world, 'region', 'r.0.0.mca')
		count = 0
		bes = 0
		for chunk_x, chunk_z, root in wa.iter_region_file_chunks(path):
			count += 1
			bes += len(root.get('block_entities') or [])
		self.assertGreater(count, 0)
		self.assertGreater(bes, 0)

	def test_find_warehouse_chests(self):
		found = {}
		path = os.path.join(self.world, 'region', 'r.0.0.mca')
		for chunk_x, chunk_z, root in wa.iter_region_file_chunks(path):
			for be in root.get('block_entities') or []:
				if be.get('id') in ('minecraft:chest', 'minecraft:barrel'):
					x, y, z = be['x'], be['y'], be['z']
					if -10 <= x <= 10 and 99 <= y <= 101 and -10 <= z <= 10:
						found[(x, y, z)] = be['id']
		self.assertGreaterEqual(len(found), 12, 'warehouse chests should be found in r.0.0.mca')


# ---------------------------------------------------------------------------
# 6. CSV 导出
# ---------------------------------------------------------------------------

class TestMerge(unittest.TestCase):
	"""1 秒内同一容器相同物品的多次取出合并为一条 (数量相加)。"""

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)

	def tearDown(self):
		self.tmp.cleanup()

	def _rec(self, ts_unix, item, x, z, delta, before, after):
		return ('t', ts_unix, '主仓库', 'minecraft:overworld', x, 100, z,
				'minecraft:chest', -1, item, delta, before, after, None, None, 0, None)

	def _drain(self, audit=None):
		audit = audit if audit is not None else self.audit
		out = []
		while True:
			try:
				out.append(audit.record_queue.get_nowait())
			except queue.Empty:
				return out

	@staticmethod
	def _ensure_db(audit):
		conn = sqlite3.connect(audit.db_path)
		conn.executescript(wa.DB_SCHEMA)
		conn.commit()
		conn.close()

	def test_merge_same_item_same_container(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -3, 5, 2), 1.0)
		self.audit._merge_record(self._rec(1000.5, 'minecraft:diamond', 4, 9, -2, 2, 0), 1.0)
		self.assertEqual(self.audit.record_queue.qsize(), 0, '窗口内不应立即入队')
		self.audit._flush_expired_pending(1002.0, 1.0)
		records = self._drain()
		self.assertEqual(len(records), 1, '应合并为一条')
		merged = records[0]
		self.assertEqual(merged[10], -5)     # delta 相加
		self.assertEqual(merged[11], 5)      # before 保持首次
		self.assertEqual(merged[12], 0)      # after 取最新
		self.assertEqual(merged[1], 1000.0)  # 时间保持首次

	def test_no_merge_different_items(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -1, 1, 0), 1.0)
		self.audit._merge_record(self._rec(1000.2, 'minecraft:gold_ingot', 4, 9, -1, 1, 0), 1.0)
		self.audit._flush_expired_pending(1002.0, 1.0)
		self.assertEqual(len(self._drain()), 2)

	def test_no_merge_different_containers(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -1, 1, 0), 1.0)
		self.audit._merge_record(self._rec(1000.2, 'minecraft:diamond', 5, 9, -1, 1, 0), 1.0)
		self.audit._flush_expired_pending(1002.0, 1.0)
		self.assertEqual(len(self._drain()), 2)

	def test_no_merge_beyond_window(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -1, 1, 0), 1.0)
		self.audit._merge_record(self._rec(1001.5, 'minecraft:diamond', 4, 9, -1, 1, 0), 1.0)
		self.audit._flush_expired_pending(1003.0, 1.0)
		records = self._drain()
		self.assertEqual(len(records), 2, '超过窗口应分开')

	def test_no_merge_mixed_directions(self):
		# 存入与取出即使同物品同容器也不合并
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -3, 5, 2), 1.0)
		self.audit._merge_record(self._rec(1000.2, 'minecraft:diamond', 4, 9, 2, 2, 4), 1.0)
		self.audit._flush_expired_pending(1002.0, 1.0)
		self.assertEqual(len(self._drain()), 2)

	def test_merge_disabled(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -1, 1, 0), 0)
		self.audit._merge_record(self._rec(1000.1, 'minecraft:diamond', 4, 9, -1, 1, 0), 0)
		self.assertEqual(len(self._drain()), 2, '窗口为 0 时不合并')

	def test_flush_all_pending(self):
		self.audit._merge_record(self._rec(1000.0, 'minecraft:diamond', 4, 9, -1, 1, 0), 1.0)
		self.audit._flush_all_pending()
		records = self._drain()
		self.assertEqual(len(records), 1)
		self.assertEqual(self.audit._pending_merges, {})

	def test_poll_cycle_merges_split_stack(self):
		"""同一周期内跨槽位取同一物品 -> 合并为一条 (数量相加)。"""
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		server = FakeChestServer(os.path.join(tmp.name, 'config', 'warehouse_audit'))
		audit = wa.WarehouseAudit(server)
		self.addCleanup(audit.stop)
		audit.load_config()
		audit.db_path = os.path.join(tmp.name, 'test.db')
		audit.containers = {(4, 100, 9): 'minecraft:chest'}
		audit.region_of = {(4, 100, 9): '主仓库'}
		self._ensure_db(audit)
		# 基线: 槽0 有 32 个钻石, 槽1 有 32 个
		server.chests = {(4, 100, 9): {0: ('minecraft:diamond', 32), 1: ('minecraft:diamond', 32)}}
		audit._read_players()
		audit._read_containers_and_diff()
		audit._flush_all_pending()
		self._drain()
		# 一次取走两槽各一部分 -> 应合并为 -40
		server.chests[(4, 100, 9)] = {0: ('minecraft:diamond', 22), 1: ('minecraft:diamond', 2)}
		audit._read_players()
		audit._read_containers_and_diff()
		audit._flush_all_pending()
		records = self._drain(audit)
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0][10], -40)
		self.assertEqual(records[0][9], 'minecraft:diamond')


class TestLifecycle(unittest.TestCase):
	"""服务器重启/断开时的数据安全行为。"""

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)
		self._prev_audit = wa.audit
		wa.audit = self.audit

	def tearDown(self):
		wa.audit = self._prev_audit
		self.tmp.cleanup()

	def test_initial_scan_thread_survives_unload(self):
		"""初始枚举线程在插件卸载 (audit=None) 后不崩溃 (AttributeError 回归)。"""
		with tempfile.TemporaryDirectory() as tmp:
			server = FakeServer(tmp)
			wa.audit = self._prev_audit  # 恢复全局供 on_load 使用
			wa.on_load(server, None)  # 启动 initial_scan 线程
			# 模拟插件卸载: on_unload 把全局 audit 置 None
			wa.on_unload(server)
			# 线程应安全退出 (等待 2.5s, 无 AttributeError 即通过)
			time.sleep(2.5)
			wa.on_load(server, None)
			wa.on_unload(server)

	def test_rcon_query_guard(self):
		"""RCON 断开/异常时 _rcon_query 返回 None 而非抛异常 (Bad file descriptor 回归)。"""
		# 服务端关闭 -> is_rcon_running False -> None
		self.server.rcon_responses = {}
		self.assertIsNone(self.audit._rcon_query('list'))
		# rcon_query 抛异常 -> None
		class _Boom:
			def is_rcon_running(self):
				return True
			def rcon_query(self, cmd):
				raise OSError(9, 'Bad file descriptor')
		orig = self.audit.server
		self.audit.server = _Boom()
		try:
			self.assertIsNone(self.audit._rcon_query('list'))
		finally:
			self.audit.server = orig

	def test_rebaseline_flushes_and_clears(self):
		# 挂起记录 + 旧快照
		rec = ('t', 1000.0, '主仓库', 'minecraft:overworld', 4, 100, 9,
			   'minecraft:chest', -1, 'minecraft:diamond', -1, 1, 0, None, None, 0, None)
		self.audit._merge_record(rec, 1.0)
		self.audit.snapshots = {(4, 100, 9): {0: ('minecraft:diamond', 5, None)}}
		self.audit.rebaseline()
		# 挂起记录已写入队列 (不丢)
		batch = []
		while True:
			try:
				batch.append(self.audit.record_queue.get_nowait())
			except queue.Empty:
				break
		self.assertEqual(len(batch), 1)
		# 快照已清空 -> 重新基线
		self.assertEqual(self.audit.snapshots, {})

	def test_on_server_startup_rebaselines(self):
		"""模块级 on_server_startup 会被 MCDR 自动调用 (SERVER_STARTUP 事件)。"""
		self.assertTrue(callable(wa.on_server_startup))
		self.audit.snapshots = {(4, 100, 9): {0: ('minecraft:diamond', 5, None)}}
		wa.on_server_startup(self.server)
		self.assertEqual(self.audit.snapshots, {}, '重启后应重新基线, 避免回档幽灵记录')

	def test_first_read_after_rebaseline_no_records(self):
		"""重新基线后首次读取相同内容不产生记录 (回档场景)。"""
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		server = FakeChestServer(os.path.join(tmp.name, 'config', 'warehouse_audit'))
		audit = wa.WarehouseAudit(server)
		self.addCleanup(audit.stop)
		audit.load_config()
		audit.db_path = os.path.join(tmp.name, 'test.db')
		audit.containers = {(4, 100, 9): 'minecraft:chest'}
		audit.region_of = {(4, 100, 9): '主仓库'}
		# 建立基线
		audit._read_players()
		audit._read_containers_and_diff()
		audit._flush_all_pending()
		# 模拟回档: 世界内容变化 (如物品被回档消失) + 重启
		server.chests[(4, 100, 9)] = {}
		audit.rebaseline()
		# 重新基线后首次读取: 空箱成为新基线, 不记录 "全部取出"
		audit._read_players()
		audit._read_containers_and_diff()
		audit._flush_all_pending()
		batch = []
		while True:
			try:
				batch.append(audit.record_queue.get_nowait())
			except queue.Empty:
				break
		self.assertEqual(batch, [], '回档后重新基线不应产生幽灵记录')


class TestRegionCommands(unittest.TestCase):
	"""!!wh region 指令: 手动配置监控坐标范围。"""

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)
		self._prev_audit = wa.audit
		wa.audit = self.audit

	def tearDown(self):
		wa.audit = self._prev_audit
		self.tmp.cleanup()

	def test_region_set_new(self):
		src = FakeSource()
		wa.region_set(src, {'name': '地下室', 'dim': 'overworld',
							'x1': -30, 'y1': 50, 'z1': -30, 'x2': 30, 'y2': 60, 'z2': 30})
		regions = self.audit.config['regions']
		self.assertEqual(len(regions), 2)
		new = [r for r in regions if r['name'] == '地下室'][0]
		self.assertEqual(new['dimension'], 'minecraft:overworld')
		self.assertEqual(new['from'], [-30, 50, -30])
		self.assertEqual(new['to'], [30, 60, 30])
		self.assertTrue(any('已添加' in r for r in src.replies))

	def test_region_set_replaces_same_name(self):
		src = FakeSource()
		wa.region_set(src, {'name': '主仓库', 'dim': 'the_nether',
							'x1': 10, 'y1': 99, 'z1': 10, 'x2': -10, 'y2': 101, 'z2': -10})
		regions = self.audit.config['regions']
		self.assertEqual(len(regions), 1, '同名区域应更新而非新增')
		self.assertEqual(regions[0]['dimension'], 'minecraft:the_nether')
		self.assertEqual(regions[0]['from'], [-10, 99, -10], '坐标应自动排序')
		self.assertEqual(regions[0]['to'], [10, 101, 10])
		self.assertTrue(any('已更新' in r for r in src.replies))

	def test_region_set_dimension_aliases(self):
		for alias, expected in [('overworld', 'minecraft:overworld'),
								('nether', 'minecraft:the_nether'),
								('end', 'minecraft:the_end'),
								('主世界', 'minecraft:overworld'),
								('地狱', 'minecraft:the_nether'),
								('末地', 'minecraft:the_end')]:
			src = FakeSource()
			wa.region_set(src, {'name': 't_' + alias, 'dim': alias,
								'x1': 0, 'y1': 0, 'z1': 0, 'x2': 1, 'y2': 1, 'z2': 1})
			region = [r for r in self.audit.config['regions'] if r['name'] == 't_' + alias][0]
			self.assertEqual(region['dimension'], expected)

	def test_region_set_bad_dimension(self):
		before = [dict(r) for r in self.audit.config['regions']]
		src = FakeSource()
		wa.region_set(src, {'name': 'x', 'dim': 'moon',
							'x1': 0, 'y1': 0, 'z1': 0, 'x2': 1, 'y2': 1, 'z2': 1})
		self.assertEqual(self.audit.config['regions'], before, '无效维度不应修改配置')
		self.assertTrue(any('未知维度' in r for r in src.replies))

	def test_region_del(self):
		src = FakeSource()
		wa.region_del(src, {'name': '主仓库'})
		self.assertEqual(self.audit.config['regions'], [])
		self.assertTrue(any('已删除' in r for r in src.replies))

	def test_region_del_missing(self):
		before = [dict(r) for r in self.audit.config['regions']]
		src = FakeSource()
		wa.region_del(src, {'name': '不存在'})
		self.assertEqual(self.audit.config['regions'], before)
		self.assertTrue(any('找不到' in r for r in src.replies))

	def test_region_clear(self):
		# 先加一个
		wa.region_set(FakeSource(), {'name': 'A', 'dim': 'end',
									 'x1': 0, 'y1': 0, 'z1': 0, 'x2': 1, 'y2': 1, 'z2': 1})
		src = FakeSource()
		wa.region_clear(src)
		self.assertEqual(self.audit.config['regions'], [])
		self.assertTrue(any('已清空' in r for r in src.replies))

	def test_show_regions(self):
		src = FakeSource()
		wa.show_regions(src)
		self.assertTrue(any('主仓库' in r for r in src.replies))
		self.assertTrue(any('监控区域' in r for r in src.replies))

	def test_show_regions_empty(self):
		self.audit.config['regions'] = []
		src = FakeSource()
		wa.show_regions(src)
		self.assertTrue(any('没有监控区域' in r for r in src.replies))

	def test_register_includes_region(self):
		with tempfile.TemporaryDirectory() as tmp:
			server = FakeServer(tmp)
			wa.register(server)
			self.assertGreater(len(server.registered_commands), 0)

	def test_region_command_dispatch(self):
		"""端到端: 通过指令树分发 !!wh region set ... (含负数坐标)。"""
		from mcdreforged.command.builder.callback import DirectCallbackInvoker

		def run_cmd(root, src, command):
			for execution in root._entry_execute(src, command):
				execution.scheduled_callback.invoke(DirectCallbackInvoker())

		with tempfile.TemporaryDirectory() as tmp:
			server = FakeServer(tmp)
			wa.register(server)
			root = server.registered_commands[0]
			wa.audit = self.audit
			try:
				src = FakeSource()
				run_cmd(root, src, '!!wh region set 主仓库 overworld -20 99 -20 20 101 20')
				regions = self.audit.config['regions']
				self.assertEqual(len(regions), 1)
				self.assertEqual(regions[0]['from'], [-20, 99, -20], '坐标应更新')
				self.assertEqual(regions[0]['to'], [20, 101, 20])
				self.assertTrue(any('已更新' in r for r in src.replies))
				# 列出区域
				src2 = FakeSource()
				run_cmd(root, src2, '!!wh region')
				self.assertTrue(any('主仓库' in r for r in src2.replies))
				# 新增区域 (负坐标 + 别名维度)
				src3 = FakeSource()
				run_cmd(root, src3, '!!wh region set 地狱仓库 nether -200 30 -200 -180 40 -180')
				self.assertEqual(len(self.audit.config['regions']), 2)
				self.assertEqual(self.audit.config['regions'][1]['dimension'], 'minecraft:the_nether')
				self.assertTrue(any('已添加' in r for r in src3.replies))
				# 删除
				src4 = FakeSource()
				run_cmd(root, src4, '!!wh region del 地狱仓库')
				self.assertEqual(len(self.audit.config['regions']), 1)
				self.assertTrue(any('已删除' in r for r in src4.replies))
				# 无效维度
				src5 = FakeSource()
				run_cmd(root, src5, '!!wh region set x moon 0 0 0 1 1 1')
				self.assertTrue(any('未知维度' in r for r in src5.replies))
				self.assertEqual(len(self.audit.config['regions']), 1)
				# 清空
				src6 = FakeSource()
				run_cmd(root, src6, '!!wh region clear')
				self.assertEqual(self.audit.config['regions'], [])
			finally:
				wa.audit = self._prev_audit


class TestExport(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.audit, self.server = make_audit(self.tmp.name, cleanup=self.addCleanup)
		# 造 50 条假数据
		conn = sqlite3.connect(self.audit.db_path)
		conn.executescript(wa.DB_SCHEMA)
		now = time.time()
		rows = []
		for i in range(50):
			rows.append((
				'2026-10-18 15:{:02d}:00'.format(i), now - i * 60, '主仓库',
				'minecraft:overworld', 4 + i % 6, 100, 9 + i % 2, 'minecraft:chest',
				-1, 'minecraft:diamond' if i % 2 == 0 else 'minecraft:gold_ingot',
				-1 if i % 3 else 2, 5, 4, 'Steve' if i % 4 else None, 1.5, i % 5 == 0,
				'packed_ice x64' if i == 0 else None))
		conn.executemany(wa.INSERT_SQL, rows)
		conn.commit()
		conn.close()

	def tearDown(self):
		self.tmp.cleanup()

	def test_export_all(self):
		out = os.path.join(self.tmp.name, 'all.csv')
		count, span_from, span_to = self.audit._write_csv(self.audit._parse_export_args(''), out)
		self.assertEqual(count, 50)
		self.assertIsNotNone(span_from)
		self.assertIsNotNone(span_to)
		with open(out, 'rb') as f:
			head = f.read(3)
		self.assertEqual(head, b'\xef\xbb\xbf', 'CSV must start with UTF-8 BOM')
		with open(out, encoding='utf-8-sig') as f:
			lines = f.read().splitlines()
		# 备注行 (# 开头) 在表头之前
		remarks = [l for l in lines if l.startswith('#')]
		self.assertGreaterEqual(len(remarks), 3)
		self.assertTrue(any('统计时间跨度' in r for r in remarks))
		self.assertTrue(any('服务端版本' in r and '1.20.4' in r for r in remarks))
		self.assertTrue(any('导出时间' in r for r in remarks))
		data_lines = [l for l in lines if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		self.assertEqual(rows[0], wa.csv_columns(False, False))
		self.assertEqual(len(rows), 51)
		self.assertIn(rows[1][9], ('存入', '取出'))

	def test_export_with_player_columns(self):
		self.audit.config['record_player_id'] = True
		out = os.path.join(self.tmp.name, 'p.csv')
		self.audit._write_csv(self.audit._parse_export_args(''), out)
		with open(out, encoding='utf-8-sig') as f:
			data_lines = [l for l in f.read().splitlines() if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		self.assertEqual(rows[0], wa.csv_columns(True, False))
		for row in rows[1:]:
			if row[10]:
				self.assertEqual(row[10], 'Steve')

	def test_export_with_dimension_column(self):
		self.audit.config['record_dimension'] = True
		out = os.path.join(self.tmp.name, 'd.csv')
		self.audit._write_csv(self.audit._parse_export_args(''), out)
		with open(out, encoding='utf-8-sig') as f:
			data_lines = [l for l in f.read().splitlines() if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		self.assertEqual(rows[0], wa.csv_columns(False, True))
		for row in rows[1:]:
			self.assertEqual(row[1], 'minecraft:overworld')

	def test_export_filter_player(self):
		out = os.path.join(self.tmp.name, 'p.csv')
		count, _, _ = self.audit._write_csv(self.audit._parse_export_args('--player Steve'), out)
		with open(out, encoding='utf-8-sig') as f:
			data_lines = [l for l in f.read().splitlines() if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		self.assertGreater(count, 0)
		self.assertEqual(len(rows) - 1, count)

	def test_export_filter_item(self):
		out = os.path.join(self.tmp.name, 'i.csv')
		self.audit._write_csv(self.audit._parse_export_args('--item minecraft:diamond'), out)
		with open(out, encoding='utf-8-sig') as f:
			data_lines = [l for l in f.read().splitlines() if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		for row in rows[1:]:
			self.assertEqual(row[5], 'minecraft:diamond')

	def test_export_filter_days(self):
		out = os.path.join(self.tmp.name, 'd.csv')
		count, _, _ = self.audit._write_csv(self.audit._parse_export_args('--days 1'), out)
		self.assertGreater(count, 0)

	def test_export_filter_container(self):
		out = os.path.join(self.tmp.name, 'c.csv')
		self.audit._write_csv(self.audit._parse_export_args('--container 5,100,10'), out)
		with open(out, encoding='utf-8-sig') as f:
			data_lines = [l for l in f.read().splitlines() if not l.startswith('#')]
		rows = list(csv.reader(data_lines))
		for row in rows[1:]:
			self.assertEqual((int(row[1]), int(row[2]), int(row[3])), (5, 100, 10))

	def test_export_limit(self):
		out = os.path.join(self.tmp.name, 'l.csv')
		count, _, _ = self.audit._write_csv(self.audit._parse_export_args('--limit 10'), out)
		self.assertEqual(count, 10)

	def test_arg_parsing_windows_path(self):
		# 无空格的反斜杠路径不需要引号, 且反斜杠必须保留
		opts = self.audit._parse_export_args(r'--out D:\some\out.csv --limit 5')
		self.assertEqual(opts['out'], r'D:\some\out.csv')
		self.assertEqual(opts['limit'], 5)
		# 含空格的路径需要引号
		opts = self.audit._parse_export_args('--out "D:\\some dir\\out.csv" --limit 5')
		self.assertEqual(opts['out'], 'D:\\some dir\\out.csv')

	def test_arg_parsing_quoted(self):
		opts = self.audit._parse_export_args('--player "Steve Head" --days 3')
		self.assertEqual(opts['player'], 'Steve Head')
		self.assertIsNotNone(opts['from_ts'])

	def test_arg_parsing_errors(self):
		with self.assertRaises(ValueError):
			self.audit._parse_export_args('--bogus 1')
		with self.assertRaises(ValueError):
			self.audit._parse_export_args('--container 1,2')

	def test_count_rows(self):
		self.assertEqual(self.audit._count_rows(), 50)


# ---------------------------------------------------------------------------
# 7. 枚举逻辑 (模拟 rcon)
# ---------------------------------------------------------------------------

class TestEnumeration(unittest.TestCase):
	def test_rcon_scan_finds_chests(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		audit, server = make_audit(tmp.name)
		self.addCleanup(audit.stop)
		# 区域 (-1..1, 99..101, -1..1), 只有 (0,100,0) 是箱子
		responses = {}
		for x in range(-1, 2):
			for y in range(99, 102):
				for z in range(-1, 2):
					pos = (x, y, z)
					if pos == (0, 100, 0):
						responses['data get block {} {} {} id'.format(*pos)] = \
							block_data_response(pos, '"minecraft:chest"')
					else:
						responses['data get block {} {} {} id'.format(*pos)] = \
							'The target block is not a block entity'
		server.rcon_responses.update(responses)
		audit.config['regions'] = [
			{'name': '测试', 'dimension': 'minecraft:overworld', 'from': [-1, 99, -1], 'to': [1, 101, 1]}]
		count = audit.rescan_containers()
		self.assertEqual(count, 1)
		with audit._state_lock:
			self.assertIn((0, 100, 0), audit.containers)
		self.assertEqual(audit.stats['last_rescan_method'], 'rcon_scan')

	def test_rcon_scan_unloaded_fallback(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		audit, server = make_audit(tmp.name)
		self.addCleanup(audit.stop)
		audit.world_dir = None  # 区域文件不可用 -> 无法补齐
		responses = {}
		for x in range(-1, 2):
			for z in range(-1, 2):
				pos = (x, 100, z)
				if pos == (0, 100, 0):
					responses['data get block {} {} {} id'.format(*pos)] = \
						block_data_response(pos, '"minecraft:barrel"')
				else:
					responses['data get block {} {} {} id'.format(*pos)] = 'That position is not loaded'
		server.rcon_responses.update(responses)
		audit.config['regions'] = [
			{'name': '测试', 'dimension': 'minecraft:overworld', 'from': [-1, 100, -1], 'to': [1, 100, 1]}]
		count = audit.rescan_containers()
		# 其余 8 块未加载且无区域文件可补, 只有 barrel 入列
		self.assertEqual(count, 1)
		with audit._state_lock:
			self.assertIn((0, 100, 0), audit.containers)

	def test_rcon_scan_finds_shulker_boxes(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		audit, server = make_audit(tmp.name)
		self.addCleanup(audit.stop)
		# 默认容器列表应包含全部潜影盒变体
		for variant in ('minecraft:shulker_box', 'minecraft:red_shulker_box',
						'minecraft:light_blue_shulker_box', 'minecraft:black_shulker_box'):
			self.assertIn(variant, audit.config['container_blocks'])
		# 区域 (-1..1, 100, -1..1): (0,100,0) 是潜影盒, (1,100,1) 是红潜影盒
		responses = {}
		for x in range(-1, 2):
			for z in range(-1, 2):
				pos = (x, 100, z)
				cmd = 'data get block {} {} {} id'.format(*pos)
				if pos == (0, 100, 0):
					responses[cmd] = block_data_response(pos, '"minecraft:shulker_box"')
				elif pos == (1, 100, 1):
					responses[cmd] = block_data_response(pos, '"minecraft:red_shulker_box"')
				else:
					responses[cmd] = 'The target block is not a block entity'
		server.rcon_responses.update(responses)
		audit.config['regions'] = [
			{'name': '测试', 'dimension': 'minecraft:overworld', 'from': [-1, 100, -1], 'to': [1, 100, 1]}]
		count = audit.rescan_containers()
		self.assertEqual(count, 2)
		with audit._state_lock:
			self.assertEqual(audit.containers.get((0, 100, 0)), 'minecraft:shulker_box')
			self.assertEqual(audit.containers.get((1, 100, 1)), 'minecraft:red_shulker_box')

	def test_empty_rescan_keeps_old_list(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		audit, server = make_audit(tmp.name)
		self.addCleanup(audit.stop)
		audit.containers = {(1, 2, 3): 'minecraft:chest'}
		# 所有块都不是容器
		server.rcon_responses['data get block 0 100 0 id'] = 'The target block is not a block entity'
		audit.config['regions'] = [
			{'name': '测试', 'dimension': 'minecraft:overworld', 'from': [0, 100, 0], 'to': [0, 100, 0]}]
		audit.rescan_containers()
		with audit._state_lock:
			self.assertIn((1, 2, 3), audit.containers, 'empty rescan must keep old list')


# ---------------------------------------------------------------------------
# 8. 完整轮询周期 (模拟 rcon_query 的服务端行为)
# ---------------------------------------------------------------------------

class FakeChestServer(FakeServer):
	"""模拟一个有 2 个箱子 + 1 名玩家的服务端。"""

	def __init__(self, data_folder):
		super().__init__(data_folder)
		self.chests = {
			(4, 100, 9): {0: ('minecraft:diamond', 5)},
			(5, 100, 9): {},
		}
		self.players = {'Steve': ('minecraft:overworld', 4.5, 100.0, 10.5)}
		self.commands = []

	def rcon_query(self, command):
		self.commands.append(command)
		if command == 'execute as @a run data get entity @s Pos':
			return 'Steve has the following entity data: [{}, {}, {}]'.format(
				repr(self.players['Steve'][1]) + 'd', repr(self.players['Steve'][2]) + 'd',
				repr(self.players['Steve'][3]) + 'd')
		if command == 'execute as @a run data get entity @s Dimension':
			return 'Steve has the following entity data: "minecraft:overworld"'
		if command.startswith('data get block ') and command.endswith(' Items'):
			parts = command.split()
			pos = (int(parts[3]), int(parts[4]), int(parts[5]))
			if pos not in self.chests:
				return 'The target block is not a block entity'
			items = self.chests[pos]
			if not items:
				return '{}, {}, {} has the following block data: []'.format(*pos)
			payload = '[{}]'.format(', '.join(
				'{{Slot: {}b, id: "{}", Count: {}b}}'.format(slot, iid, cnt)
				for slot, (iid, cnt) in sorted(items.items())))
			return '{}, {}, {} has the following block data: {}'.format(pos[0], pos[1], pos[2], payload)
		if command.startswith('data get block ') and command.endswith(' id'):
			parts = command.split()
			pos = (int(parts[3]), int(parts[4]), int(parts[5]))
			if pos in self.chests:
				return '{}, {}, {} has the following block data: "minecraft:chest"'.format(*pos)
			return 'The target block is not a block entity'
		return None


class TestPollCycle(unittest.TestCase):
	def test_end_to_end_take_and_put(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		server = FakeChestServer(os.path.join(tmp.name, 'config', 'warehouse_audit'))
		audit = wa.WarehouseAudit(server)
		self.addCleanup(audit.stop)
		audit.load_config()
		# 完整模式: 记录存入 + 记录玩家ID
		audit.config['record_deposits'] = True
		audit.config['record_player_id'] = True
		audit.db_path = os.path.join(tmp.name, 'test.db')
		audit.export_dir = os.path.join(tmp.name, 'export')
		self._ensure_db(audit)
		# 预置容器列表 (跳过真实枚举)
		audit.containers = {(4, 100, 9): 'minecraft:chest', (5, 100, 9): 'minecraft:chest'}
		audit.region_of = {(4, 100, 9): '主仓库', (5, 100, 9): '主仓库'}
		# 第一周期: 建立基线, 无记录
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		# 玩家从 (4,100,9) 取走 2 个钻石
		server.chests[(4, 100, 9)] = {0: ('minecraft:diamond', 3)}
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		rows = conn.execute(
			'SELECT player, item_id, delta, before_count, after_count, x, y, z '
			'FROM container_changes').fetchall()
		conn.close()
		self.assertEqual(len(rows), 1, 'should record exactly one change')
		player, item_id, delta, before, after, x, y, z = rows[0]
		self.assertEqual(player, 'Steve')
		self.assertEqual(item_id, 'minecraft:diamond')
		self.assertEqual(delta, -2)
		self.assertEqual((before, after), (5, 3))
		self.assertEqual((x, y, z), (4, 100, 9))

		# 玩家往 (5,100,9) 放入 64 个木板
		server.chests[(5, 100, 9)] = {3: ('minecraft:oak_planks', 64)}
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		rows = conn.execute(
			'SELECT player, item_id, delta FROM container_changes WHERE item_id = ?',
			('minecraft:oak_planks',)).fetchall()
		conn.close()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][2], 64)

		# 箱子被拆除 -> 记录剩余物品全部取出
		del server.chests[(4, 100, 9)]
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		rows = conn.execute(
			'SELECT item_id, delta, after_count FROM container_changes WHERE x = 4').fetchall()
		conn.close()
		self.assertEqual(len(rows), 2)  # 第一次 -2, 第二次 -3
		self.assertEqual(sum(r[1] for r in rows), -5)

	def test_default_mode_take_only_no_player(self):
		"""默认配置 (record_deposits=False, record_player_id=False): 只记取出, 不查玩家。"""
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		server = FakeChestServer(os.path.join(tmp.name, 'config', 'warehouse_audit'))
		audit = wa.WarehouseAudit(server)
		self.addCleanup(audit.stop)
		audit.load_config()
		self.assertFalse(audit.config['record_deposits'])
		self.assertFalse(audit.config['record_player_id'])
		audit.db_path = os.path.join(tmp.name, 'test.db')
		audit.containers = {(4, 100, 9): 'minecraft:chest'}
		audit.region_of = {(4, 100, 9): '主仓库'}
		self._ensure_db(audit)
		# 基线
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		self.assertEqual(audit.players, {}, 'players must not be queried by default')
		# 往空槽位存入 3 个金锭 -> 不记录
		server.chests[(4, 100, 9)] = {0: ('minecraft:diamond', 5), 1: ('minecraft:gold_ingot', 3)}
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		count = conn.execute('SELECT COUNT(*) FROM container_changes').fetchone()[0]
		conn.close()
		self.assertEqual(count, 0, 'deposits must not be recorded by default')
		# 取出 1 个金锭 -> 记录, player 为 NULL
		server.chests[(4, 100, 9)] = {0: ('minecraft:diamond', 5), 1: ('minecraft:gold_ingot', 2)}
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		rows = conn.execute(
			'SELECT player, item_id, delta, before_count, after_count '
			'FROM container_changes').fetchall()
		conn.close()
		self.assertEqual(len(rows), 1)
		self.assertIsNone(rows[0][0], 'player must be NULL by default')
		self.assertEqual(rows[0][1], 'minecraft:gold_ingot')
		self.assertEqual(rows[0][2], -1)

	def test_unloaded_chunk_skipped(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		server = FakeChestServer(os.path.join(tmp.name, 'config', 'warehouse_audit'))
		audit = wa.WarehouseAudit(server)
		self.addCleanup(audit.stop)
		audit.load_config()
		audit.db_path = os.path.join(tmp.name, 'test.db')
		audit.containers = {(4, 100, 9): 'minecraft:chest'}
		audit.region_of = {(4, 100, 9): '主仓库'}
		self._ensure_db(audit)
		# 第一周期基线
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		# 模拟区块未加载: 覆写 rcon_query
		orig = server.rcon_query

		def fake_query(cmd):
			if cmd.startswith('data get block') and cmd.endswith('Items'):
				return 'That position is not loaded'
			return orig(cmd)
		server.rcon_query = fake_query
		# 同时内容变化 (理论上不可能, 但插件必须不记录)
		server.chests[(4, 100, 9)] = {}
		audit._read_players()
		audit._read_containers_and_diff()
		self._drain(audit, server)
		conn = sqlite3.connect(audit.db_path)
		count = conn.execute('SELECT COUNT(*) FROM container_changes').fetchone()[0]
		conn.close()
		self.assertEqual(count, 0, 'unloaded chunk must be skipped, no records')

	@staticmethod
	def _ensure_db(audit):
		conn = sqlite3.connect(audit.db_path)
		conn.executescript(wa.DB_SCHEMA)
		conn.commit()
		conn.close()

	@staticmethod
	def _drain(audit, server):
		"""把队列中的记录直接写库 (测试中不起写库线程; 先刷出合并缓冲)。"""
		audit._flush_all_pending()
		batch = []
		while True:
			try:
				batch.append(audit.record_queue.get_nowait())
			except Exception:
				break
		if batch:
			audit._write_batch(batch)


# ---------------------------------------------------------------------------
# 9. 指令与插件入口
# ---------------------------------------------------------------------------

class TestCommands(unittest.TestCase):
	def test_register_command_tree(self):
		with tempfile.TemporaryDirectory() as tmp:
			server = FakeServer(tmp)
			wa.register(server)  # 不应抛异常

	def test_on_load_registers_commands(self):
		"""回归测试: on_load 必须显式调用 register() —— MCDR 不会自动调用模块级 register。"""
		with tempfile.TemporaryDirectory() as tmp:
			server = FakeServer(tmp)
			server._running = False  # 阻止轮询线程真正跑起来
			server._rcon = False
			wa.on_load(server, None)
			try:
				self.assertGreater(len(server.registered_commands), 0,
								   'on_load must call register() to register commands')
				self.assertGreater(len(server.help_messages), 0,
								   'on_load must register the help message')
			finally:
				wa.on_unload(server)
			self.assertIsNone(wa.audit)

	def test_status_lines(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			lines = audit.status_lines()
			self.assertTrue(any('仓库审计日志' in line for line in lines))

	def test_export_command_no_db(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			audit.db_path = os.path.join(tmp, 'nonexistent.db')
			src = FakeSource()
			audit.export_csv('', src)
			self.assertTrue(any('尚不存在' in r for r in src.replies))

	def test_export_command_bad_args(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			# 建一个空数据库
			conn = sqlite3.connect(audit.db_path)
			conn.executescript(wa.DB_SCHEMA)
			conn.commit()
			conn.close()
			src = FakeSource()
			audit.export_csv('--bogus', src)
			self.assertTrue(any('参数错误' in r for r in src.replies))

	def test_container_list_empty(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			lines = audit.container_list_lines()
			self.assertTrue(any('尚未枚举' in line for line in lines))

	def test_recent_records_lines(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			# 无数据库
			lines = audit.recent_records_lines()
			self.assertTrue(any('尚不存在' in line for line in lines))
			# 造 25 条数据
			conn = sqlite3.connect(audit.db_path)
			conn.executescript(wa.DB_SCHEMA)
			now = time.time()
			rows = []
			for i in range(25):
				rows.append((
					'2026-10-18 16:{:02d}:00'.format(i), now - (25 - i) * 60, '主仓库',
					'minecraft:overworld', 4, 100, 9, 'minecraft:chest',
					-1, 'minecraft:diamond', -1, 5, 4, None, None, 0,
					'observer x64' if i == 24 else None))
			conn.executemany(wa.INSERT_SQL, rows)
			conn.commit()
			conn.close()
			lines = audit.recent_records_lines(10)
			self.assertEqual(len(lines), 11)  # 1 标题 + 10 条
			self.assertIn('最近 10 条', lines[0])
			# 最新在上: 第一条记录应是 i=24 (16:24:00)
			self.assertIn('16:24:00', lines[1])
			self.assertIn('diamond', lines[1])
			# 内容物摘要应显示
			self.assertIn('observer x64', lines[1])
			# 默认不记录玩家 -> 不含玩家名
			self.assertNotIn('Steve', ' '.join(lines))
			# 开启玩家记录后应包含玩家列
			audit.config['record_player_id'] = True
			conn = sqlite3.connect(audit.db_path)
			conn.execute("UPDATE container_changes SET player='Steve'")
			conn.commit()
			conn.close()
			lines = audit.recent_records_lines(3)
			self.assertTrue(any('Steve' in line for line in lines))

	def test_recent_records_empty_db(self):
		with tempfile.TemporaryDirectory() as tmp:
			audit, server = make_audit(tmp)
			conn = sqlite3.connect(audit.db_path)
			conn.executescript(wa.DB_SCHEMA)
			conn.commit()
			conn.close()
			lines = audit.recent_records_lines()
			self.assertTrue(any('暂无记录' in line for line in lines))


if __name__ == '__main__':
	unittest.main(verbosity=2)
