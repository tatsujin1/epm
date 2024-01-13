import sys
import time
import os
from datetime import datetime, timedelta
from os.path import dirname, exists as pexists
from subprocess import run, PIPE, Popen
import shutil
import io
from tempfile import mkstemp
import enum

from . import config
from .config import debug
from .utils import read_json_obj, write_json, now_datetime
from .styles import _0, _f, _E, _00

from typing import Any, Callable, TypeVar, Generator

DB_VERSION = 4

_dirty = True

def is_dirty() -> bool:
	return _dirty

def set_dirty(dirty:bool=True):
	global _dirty
	_dirty = dirty


def code_version() -> int:
	return DB_VERSION

def base_filename():
	return str(config.get('paths/series-db'))

def active_file(uncompressed:bool=False) -> str:
	return _filename_slot(base_filename(), 0)


def load(db_file:str|None=None) -> dict:

	if not db_file:
		db_file = active_file()

	if not db_file or not isinstance(db_file, str) or len(db_file) < 2:
		raise RuntimeError('Invalid series db file path: %r' % db_file)


	if not pexists(db_file):
		debug('db: standard file doesn\'t exist: %s' % db_file)

		if _compressor:
			# also try the uncompressed filename
			# TODO: actually, we need the uncompressed variant of 'db_file'
			uncompressed_file = str(config.get('paths/series-db'))

		debug('db: trying uncompressed file: %s' % uncompressed_file)
		if pexists(uncompressed_file):
			t0 = time.time()
			mk_backup(uncompressed_file, db_file)
			t1 = time.time()
			ms = (t1 - t0)*1000
			debug(f'db: compressed uncompressed file: {uncompressed_file} in %.1fms' % ms)

		if not pexists(db_file):
			# try old location
			old_db_file = uncompressed_file.replace('/episode_manager/', '/epm/')
			debug('db: trying old location: %s' % old_db_file)
			if pexists(old_db_file):
				os.makedirs(dirname(uncompressed_file), exist_ok=True)
				shutil.copy(old_db_file, uncompressed_file)
				print(f'{_f}[db: copied from old location: {old_db_file} -> {uncompressed_file}]{_0}')


	if not pexists(db_file):
		# brand new database
		print(f'{_f}[db: new database]{_0}')
		return {}


	t0 = time.time()
	db = read_json_obj(compressed_open(db_file))
	t1 = time.time()

	if debug:
		ms = (t1 - t0)*1000
		debug(f'{_f}db: read %d entries in %.1fms; v%d{_0}' % (len(db) - 1, ms, meta_get(db, meta_version_key)))

	set_dirty(False)

	_migrate(db)

	if is_dirty():
		save(db)

	return db


def _migrate(db:dict):
	# no db meta data, yikes!
	if meta_key not in db:
		db[meta_key] = {}
		set_dirty()

	db_version = db[meta_key].get('version', 0)

	fixed_legacy_meta = 0
	fixed_archived = 0
	fixed_update_history = 0
	fixed_nulls = 0
	fixed_update_history_dups = 0

	for series_id in all_ids(db):
		series = db[series_id]

		if db_version < 1:
			if meta_key not in series:
				series[meta_key] = {
						key: series.pop(key)
						for key in meta_legacy_keys
						if key in series
				}
				fixed_legacy_meta += 1

			if meta_get(series, meta_archived_key) == True:
				# fix all "archived" values to be dates (not booleans)
				seen = meta_get(series, meta_seen_key)
				last_seen = '0000-00-00 00:00:00'
				# use datetime from last marked episode
				for dt in seen.values():
					if dt > last_seen:
						last_seen = dt

				meta_set(series, meta_archived_key, last_seen)
				fixed_archived += 1

		if db_version < 3:
			last_update = meta_get(series, 'updated')
			if last_update:
				meta_set(series, meta_update_check_key, last_update)
				meta_del(series, 'updated')

			update_history = meta_get(series, meta_update_history_key)
			if not update_history and last_update:
				meta_set(series, meta_update_history_key, [last_update])
				fixed_update_history += 1

			series.pop('id', None)

		if db_version < 4:
			def _del_empty(data):
				nonlocal fixed_nulls
				if isinstance(data, list):
					for item in data:
						_del_empty(item)

				elif isinstance(data, dict):
					for key, value in list(data.items()):
						if value is None:
							del data[key]
							fixed_nulls += 1
						elif isinstance(value, (dict, list)):
							_del_empty(value)

			_del_empty(series)

		history = meta_get(series, meta_update_history_key)
		if len(history) >=2:
			history.sort()
			idx = 0
			mods = 0
			while len(history) >= 2 and idx < len(history):
				if idx > 0 and history[idx - 1] == history[idx]:
					del history[idx - 1]
					mods += 1
				else:
					idx += 1

			if mods > 0:
				debug('Removed dup %d history items from %s' % (mods, series['title']))
				fixed_update_history_dups += mods

	if db_version < 2:
		# assign list index in added time order
		list_index = 1
		for series in sorted(db.values(), key=lambda series: meta_get(series, meta_added_key)):
			meta_set(series, meta_list_index_key, list_index)
			list_index += 1
		set_dirty()

	# if no version exists, set to current version
	if db_version != DB_VERSION:
		print(f'{_f}Set DB version: %s -> %s{_0}' % (meta_get(db, meta_version_key), DB_VERSION))
		meta_set(db, meta_version_key, DB_VERSION)

	if db_version < 2:
		meta_set(db, meta_next_list_index_key, list_index)
		print(f'{_f}Built list indexes for all {len(db) - 1} series, next index: {list_index}{_0}')

	if fixed_legacy_meta:
		print(f'{_f}Migrated legacy meta-data of {fixed_legacy_meta} series{_0}')
		set_dirty()

	if fixed_archived:
		print(f'{_f}Fixed bad "{meta_archived_key}" value of {fixed_archived} series{_0}')
		set_dirty()

	if fixed_update_history:
		print(f'{_f}Fixed empty "{meta_update_history_key}" value of {fixed_update_history} series{_0}')
		set_dirty()

	if fixed_nulls:
		print(f'{_f}Removed {fixed_nulls} null values{_0}')
		set_dirty()

	if fixed_update_history_dups:
		print(f'{_f}Removed {fixed_update_history_dups} duplicate update history entries{_0}')
		set_dirty()

_compressors:list[dict[str, str | list[str]]] = [
    {
	    'binary': 'zstd',
		'extension': '.zst',
		'args': ['-15', '--quiet', '--threads=0'],
		'unargs': ['--decompress', '--quiet', '--threads=0'],
		'pipe': ['--stdout'],
	},
	{
	    'binary': 'lz4',
		'extension': '.lz4',
		'args': ['-9', '--quiet'],
		'unargs': ['--decompress', '--quiet'],
		'pipe': ['--stdout'],
	},
	{
	    'binary': 'xz',
		'extension': '.xz',
		'args': ['-5', '--quiet'],
		'unargs': ['--decompress', '--quiet'],
		'pipe': ['--stdout'],
	},
	{
	    'binary': 'plzip',
		'extension': '.lz',
		'args': ['-5', '--quiet'],  # parallel by default
		'unargs': ['--decompress', '--quiet'],
		'pipe': ['--stdout'],
	},
	{
	    'binary': 'lzip',
		'extension': '.lz',
		'args': ['-5', '--quiet'],
		'unargs': ['--decompress', '--quiet'],
		'pipe': ['--stdout'],
	},
	{
	    'binary': 'gzip',
		'extension': '.gz',
		'args': ['-9', '--quiet'],
		'unargs': ['--decompress', '--quiet'],
		'pipe': ['--stdout'],
	},
]

# detect which of the above compressor are available (in order of desirability)
_compressor:dict|None = None
for method in _compressors:
	if shutil.which(method['binary']):
		_compressor = method
		break

if not _compressor:
	raise RuntimeError('no compressor available (tried: %s)' % (', '.join(c['binary'] for c in _compressors)))

def compressor():
	return _compressor['binary'] if _compressor else None

# TODO: 'mk_backup' should return a waitable promise (so we can do it in parallel with the serialization)

def _run_compressor(source:str, destination:str|None, compress:bool=True) -> bool|io.BufferedReader:

	# copy file access & mod timestamps from source
	source_info = os.stat(source)

	success = False  # always assume failure  ;)

	args = _compressor['args' if compress else 'unargs']
	command_line = [_compressor['binary']] + args # type: ignore

	try:
		infp = open(source, 'rb')
		outfp = open(destination, 'wb')

		comp = run(command_line, stdin=infp, stdout=outfp)
		success = comp.returncode == 0

		infp.close()
		outfp.close()

		if not success:
			raise RuntimeError('exit code: %d' % comp.returncode)

		# file compressed into destination

		# we can safely remove the source(s)
		os.remove(source)
		# copy timestamps from source file
		os.utime(destination, (source_info.st_atime, source_info.st_mtime))

	except Exception as e:
		# (de)compression failed, just fall back to uncomrpessed
		verb = 'Compressing' if compress else 'Decompressing'

		print(f'{_E}ERROR{_00} {verb} file failed: {e}')

	return success


def _mk_uncompressed_backup(source:str, destination:str) -> bool:
	os.rename(source, destination)
	return True

def _unmk_uncompressed_backup(source:str, destination:str) -> bool:
	os.rename(source, destination)
	return True

def mk_backup(source:str, destination:str) -> bool:
	if not _compressor or not _run_compressor(source, destination, compress=True):
		_mk_uncompressed_backup(source, destination)
		return False

	return True

def unmk_backup(source:str, destination:str) -> bool:
	if not _compressor or not _run_compressor(source, destination, compress=False):
		_unmk_uncompressed_backup(source, destination)
		return False

	return True


def compress_file(source:str, destination:str) -> bool:
	if not _compressor:
		os.rename(source, destination)
		return True

	return _run_compressor(source, destination, compress=True)


def compressed_open(source:str) -> io.BufferedReader:
	if not _compressor:
		return open(source, 'rb')
	command_line = [_compressor['binary']] + _compressor['unargs'] + _compressor['pipe'] # type: ignore
	return Popen(command_line, stdin=open(source, 'rb'), stdout=PIPE).stdout


def _filename_slot(base_name:str, idx:int) -> str:
	if _compressor:
		return '%s.%d%s' % (base_name, idx, _compressor['extension'])

	return '%s.%d' % (base_name, idx)


def _rotate_backups(base_name:str):
	for idx in range(config.get_int('num-backups') - 1, 0, -1):
		org_file = _filename_slot(base_name, idx)
		if pexists(org_file):
			shifted_file = _filename_slot(base_name, idx + 1)
			os.rename(org_file, shifted_file)


def save(db:dict) -> None:

	if not is_dirty():
		debug(f'{_f}db: save ignored; not dirty{_0}')
		return

	set_dirty(False)

	base_name = base_filename()
	db_path = dirname(base_name)

	os.makedirs(db_path, exist_ok=True)

	# write to a temp file and then rename it afterwards
	tmp_name = mkstemp(dir=db_path)[1]

	t0 = time.time()
	err = write_json(tmp_name, db)

	if err is not None:
		print(f'{_E}ERROR{_00} Failed saving series database: %s' % str(err), file=sys.stderr)
		os.remove(tmp_name)
		return

	# current file becomes first backup (<name>.1)
	# TODO: spawn background process to compress to make it appear faster?
	#   might run into (more) race-conditions of course

	# backup active files to the '.1' backup slot
	os.rename(active_file(), _filename_slot(base_name, 1))

	tmp_name2 = mkstemp(dir=db_path)[1]
	if not compress_file(tmp_name, tmp_name2):
		os.remove(tmp_name)
		os.remove(tmp_name2)
		return

	_rotate_backups(base_name)

	os.rename(tmp_name2, active_file())
	t1 = time.time()

	if debug:
		ms = (t1 - t0)*1000
		debug(f'{_f}db: wrote %d entries in %.1fms; v%d{_0}' % (len(db) - 1, ms, meta_get(db, meta_version_key)))


def list_backups() -> list[str]:
	"""Returns a list of existing backups, most recent first."""

	base_name = base_filename()

	bups = []

	for idx in range(1, config.get_int('num-backups') + 1):
		bup_name = _filename_slot(base_name, idx)
		if pexists(bup_name):
			bups.append(bup_name)

	return bups


def rollback():
	"""Restore the most recent backup and shift all backups indices"""

	db_file = active_file()

	first_backup = _filename_slot(db_file, 1)
	if not pexists(first_backup):
		return None, f'Backup "{first_backup}" does not exist', None

	change_log = meta_get(load(), meta_changes_log_key, [])

	os.rename(first_backup, db_file)

	# decreease the index of all other backups
	num_remaining = 0
	for idx in range(2, config.get_int('num-backups') + 1):
		org_file = _filename_slot(idx)
		if pexists(org_file):
			num_remaining += 1
			unshifted_file = _filename_slot(idx - 1)
			os.rename(org_file, unshifted_file)

	return num_remaining, first_backup, change_log


def meta_get(obj:dict, key:str, def_value:Any=None) -> Any:
	return obj.get(meta_key, {}).get(key, def_value)


def meta_has(obj:dict, key:str) -> bool:
	return meta_get(obj, key, None) is not None


def meta_set(obj:dict, key: str, value) -> None:
	set_dirty()

	if meta_key not in obj:
		obj[meta_key] = {}

	obj[meta_key][key] = value


def meta_del(obj:dict, key: str) -> None:
	if key in obj.get(meta_key, {}):
		set_dirty()
	obj[meta_key].pop(key, None)


def meta_copy(source:dict, destination:dict) -> None:
	set_dirty()
	destination[meta_key] = source.get(meta_key, {})


def changelog_add(obj:dict, message:str, subject:str|None=None):
	log = meta_get(obj, meta_changes_log_key, [])
	log.append((message, subject))
	meta_set(obj, meta_changes_log_key, log)
	set_dirty()

	debug('Logged change:', message, subject)


def changelog_clear(obj:dict):
	dirtyBefore = is_dirty()
	meta_del(obj, meta_changes_log_key)
	set_dirty(dirtyBefore)

class State(enum.IntFlag):
	PLANNED   = 0x01  # added but nothing seen (yet)
	STARTED   = 0x02  # some episodes seen
	COMPLETED = 0x04  # all episodes seen and manually restored
	ARCHIVED  = 0x08  # all episodes seen (automatically archived)
	ABANDONED = 0x10 | ARCHIVED  # manually archived when not all episodes seen

	ACTIVE    = PLANNED | STARTED


T = TypeVar('T')
def filter_map(db:dict, sort_key:Callable[[str, dict],Any]|None=None, filter:Callable[[str,dict],bool]|None=None, map:Callable[[str,dict],T]|None=None) -> Generator[T,None,None]:

	if filter is None:
		def no_filter(series_id:str, series:dict):
			return True
		filter = no_filter

	if map is None:
		def identity(series_id:str, series:dict):
			return series_id, series
		map = identity

	db_iter:Generator|list = (
		(series_id, series)
		for series_id, series in db.items()
		if series_id != meta_key
	)
	if sort_key:
		db_iter = sorted(db_iter, key=sort_key)  # type: ignore # 'key' expects more generic type than we use

	return (
		map(series_id, series)
		for series_id, series in db_iter
		if filter(series_id, series)
	)


def _sortkey_title_and_year(sid_series:tuple[str,dict]) -> Any:
	series_id, series = sid_series
	return series['title'].casefold(), series.get('year', [])

def indexed_series(db:dict, index=None, match=None, state:State|None=None, sort_key:Callable|None=None) -> list[tuple[int, str]]:
	"""Return a list with a predictable sorting, optionally filtered."""

	def flt(_, series:dict) -> bool:
		passed:bool = True

		if passed and index is not None:
			passed = meta_get(series, meta_list_index_key) == index

		if passed and match is not None:
			passed = match(series)

		if passed and state is not None:
			passed = (series_state(series) & state) > 0

		return passed

	def index_and_series(series_id:str, series:dict) -> tuple[int, str]:
		return meta_get(series, meta_list_index_key), series_id

	sort_key = sort_key or _sortkey_title_and_year

	return list(filter_map(db, filter=flt, map=index_and_series, sort_key=sort_key))


def find_single_series(db:dict, needle:str, filter_callback:Callable[[dict],bool]|None=None) -> tuple[int|None, str|None, str|None]:
	nothing_found = None, None, f'Series not found: {needle}'

	if not needle:
		return nothing_found

	find_index:int|None = None
	imdb_id:str|None = None
	find_title:str|None = None

	# int -> list index
	# "tt[0-9]+" -> IMDb ID
	# anything else -> title
	try:
		find_index = int(needle)
	except ValueError:
		if needle[:2] == 'tt':
			imdb_id = needle
		else:
			find_title = needle.casefold()

	if find_title:
		debug('find_title:', find_title)
	elif find_index:
		debug('find_index:', find_index)

	def flt(_, series:dict) -> bool:
		passed = True

		if passed and find_index is not None:
			passed = meta_get(series, meta_list_index_key) == find_index

		if passed and imdb_id is not None:
			passed = series.get('imdb_id') == imdb_id

		if passed and find_title is not None:
			passed = find_title in series.get('title', '').casefold()

		if passed and filter_callback is not None:
			passed = filter_callback(series)

		return passed

	def index_sid(series_id:str, series:dict) -> tuple[int, str]:
		return meta_get(series, meta_list_index_key), series_id

	found = list(filter_map(db, filter=flt, map=index_sid))

	if len(found) == 1:
		return *found[0], None

	if len(found) > 1:
		return None, None, found

	return nothing_found


def last_seen_episode(series:dict) -> tuple[dict|None, str|None]:
	episodes = series.get('episodes', [])
	if not episodes:
		return None, None

	seen = meta_get(series, meta_seen_key, {})
	last_seen = (0, 0)
	seen_time = None
	for seen_key in seen.keys():
		season, episode = seen_key.split(':')
		if season == 'S':
			continue
		season = int(season)
		episode = int(episode)
		if season > last_seen[0] or episode > last_seen[1]:
			last_seen = (season, episode)
			seen_time = seen[seen_key]

	if last_seen == (0, 0):
		return 0, None

	for ep in episodes:
		season = ep['season']
		episode = ep['episode']
		# next episode in same season or first in next season
		if season == last_seen[0] and episode == last_seen[1]:
			return ep, seen_time

	return None, None


def next_unseen_episode(series:dict) -> dict|None:

	episodes = series.get('episodes', [])
	if not episodes:
		return None

	seen = meta_get(series, meta_seen_key, {})
	last_seen = (0, 0)
	for seen_key in seen.keys():
		season, episode = [
			n if n == 'S' else int(n)
			for n in seen_key.split(':')
		]
		if season == 'S':
			continue  # only count "regular" epixodes
		if season > last_seen[0] or (season == last_seen[0] and episode > last_seen[1]):
			last_seen = (season, episode)

	if last_seen == (0, 0):
		return episodes[0]

	for ep in episodes:
		season = ep['season']
		episode = ep['episode']
		# next episode in same season (checked first) or first in next season
		if season == last_seen[0] and episode == last_seen[1] + 1 \
			or\
			season == last_seen[0] + 1 and episode == 1:
			return ep

	return None


def all_ids(db:dict) -> list[str]:
	return list(
		key
		for key in db.keys()
		if key != meta_key
	)


def series_state(series:dict) -> State:
	is_archived = meta_has(series, meta_archived_key)
	is_ended = series.get('status') in ('ended', 'canceled')

	num_episodes = len(series.get('episodes', []))
	num_seen = len(meta_get(series, meta_seen_key, {}))
	num_unseen = num_episodes - num_seen

	if is_archived:
		if num_unseen > 0:  # partially seen
			return State.ABANDONED

		return State.ARCHIVED

	elif num_seen:
		if not num_unseen and is_ended:
			return State.COMPLETED

		return State.STARTED

	return State.PLANNED


HOUR = 3600
DAY = 24*HOUR
WEEK = 7*DAY

def should_update(series:dict) -> bool:

	# never updated -> True
	# archived -> False
	# ended -> False   (assumes, as we got the "ended" status, we also got all the episodes)
	# if update history > 2, AGE = interval between last two updates, cap: 2 weeks
	# else: AGE = age of last update, cap: 2 days
	# ---
	# if last check is older than NOW - AGE: True
	# else: False

	# TODO: take seen episodes into account?

	last_check = meta_get(series, meta_update_check_key)
	if not last_check:  # no updates whatsoever
		return True

	if series_state(series) & (State.ARCHIVED | State.COMPLETED) > 0:
		return False

	debug(f'\x1b[33;1m{series["title"]}\x1b[m', end='')

	if series.get('status') in ('ended', 'canceled'):
		# it's assumed we already have all the necessary info (most importantly the episodes)
		#debug(f'  \x1b[3m{series["status"]}\x1b[m -> \x1b[31;1mFalse\x1b[m')
		debug('\r\x1b[K', end='')
		return False

	last_check = datetime.fromisoformat(last_check)
	simple_age_cap = 2*WEEK

	update_history = meta_get(series, meta_update_history_key)
	if not update_history:
		debug(' \x1b[35;1mno updates\x1b[m \x1b[32;1mTrue\x1b[m')
		return True

	# time between the last (actual) update and the last time it was checked
	last_update = datetime.fromisoformat(update_history[-1])
	capped = ''

	if len(update_history) >= 2:
		# interval between two last updates
		# TODO: average interval between all updates, or longest/shortest?
		update_interval_sum = timedelta(0)
		for idx in range(1, len(update_history)):
			update_interval_sum += datetime.fromisoformat(update_history[idx]) - datetime.fromisoformat(update_history[idx - 1])
		update_interval = update_interval_sum/(len(update_history) - 1)
		if update_interval.total_seconds() >= simple_age_cap:
			update_interval = timedelta(seconds=simple_age_cap)
			capped = 'cap'
		debug(f' history interval:{update_interval.total_seconds()/DAY:.1f}d \x1b[33;1m{capped}\x1b[m', end='')
	else:
		update_interval = now_datetime() - last_update
		if update_interval.total_seconds() >= simple_age_cap:
			update_interval = timedelta(seconds=simple_age_cap)
			capped = 'cap'
		debug(f' last interval:{update_interval.total_seconds()/DAY:.1f}d \x1b[33;1m{capped}\x1b[m', end='')

	next_update = last_check + update_interval
	debug(f'  next:{str(next_update)[:19]}', end='')

	expired = now_datetime() > next_update
	if expired:
		debug(' \x1b[32;1mTrue\x1b[m')
	else:
		debug(' \x1b[31;1mFalse\x1b[m')

	return expired



def series_seen_unseen(series:dict, before:datetime|None=None) -> tuple[list, list]:
	episodes = series.get('episodes', [])
	seen = meta_get(series, meta_seen_key, {})

	seen_eps = []
	unseen_eps = []

	for ep in episodes:
		if episode_key(ep) in seen:
			seen_eps.append(ep)
		else:
			# only include episodes in 'unseen' that are already available
			dt = ep.get('date')
			if dt:
				dt = datetime.fromisoformat(dt)
				if before and dt > before:
					continue

			elif before is not None:  # we're filtering by date but episode has no date
				continue

			unseen_eps.append(ep)

	return seen_eps, unseen_eps


def episode_key(episode:dict):
	return f'{episode["season"]}:{episode["episode"]}'


def series_index(index_number:int):
	if index_number < 100:
		return (None, str(index_number))

	low = index_number % 100
	high = index_number // 100

	high_digits = []
	while True:
		digit = high % 26
		high -= digit
		if not digit:
			break
		digit = chr(digit - 1 + ord('a'))
		high_digits.insert(0, digit)

	high_digits = ''.join(high_digits)
	low_digits = '%02d' % low

	return (high_digits, low_digits)


# def series_num_archived(db:dict) -> int:
# 	return sum(1 if meta_has(series, meta_archived_key) else 0 for series in db.values())


meta_key = 'epm:meta'
meta_added_key = 'added'
meta_seen_key = 'seen'
meta_archived_key = 'archived'
meta_list_index_key = 'list_index'
meta_next_list_index_key = 'next_list_index'
meta_update_check_key = 'update_check'
meta_update_history_key = 'update_history'
meta_rating_key = 'rating'
meta_rating_comment_key = 'rating_comment'
meta_version_key = 'version'
meta_changes_log_key = 'changes_log'
meta_add_comment_key = 'add_comment'


meta_legacy_keys = (
	meta_added_key,
	meta_update_check_key,
	meta_seen_key,
	meta_archived_key,
)
