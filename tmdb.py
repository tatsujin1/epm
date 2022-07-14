import sys
import requests
from urllib.parse import quote as url_escape
from http import HTTPStatus
import json
import concurrent.futures
from datetime import datetime, date, timedelta
import re
import os
import builtins
from typing import Callable, Any

# TODO: API version 4 ?
_base_url_tmpl = 'https://api.themoviedb.org/3/%%(path)s?api_key=%s'
_base_url:str|None = None
_api_key = os.getenv('TMDB_API_KEY', '')

api_key_help = 'Set "TMDB_API_KEY" environment variable for your account.'

class NoAPIKey(RuntimeError):
	pass


__parallel_requests = 16
__executor = None

def set_parallel(num):
	if __executor is not None:
		raise RuntimeError('Executor already initialized')

	global __parallel_requests
	__parallel_requests = max(1, int(num or 1))

def __get_executor():
	global __executor

	if __executor is None:
		__executor = concurrent.futures.ThreadPoolExecutor(max_workers=__parallel_requests, thread_name_prefix='tmdb-request')

	return __executor


def _update_query_func():
	def mk_url(endpoint:str, query:dict) -> str:
		if _base_url is None:
			raise RuntimeError('_base_url is None, which should never happen!')

		url = _base_url % { 'path': endpoint }

		if query is not None:
			q = []
			for k, v in query.items():
				q.append('%s=%s' % (url_escape(k), url_escape(str(v))))
			url += '&%s' % '&'.join(q)

		return url

	global _qurl
	_qurl = mk_url


_qurl:Callable[[str, dict], str]|None = None

def set_api_key(key):
	global _api_key
	_api_key = key

	if _api_key:
		global _base_url
		_base_url = _base_url_tmpl % _api_key
		_update_query_func()

if _api_key:
	set_api_key(_api_key)

def _query(url) -> dict[str, Any]|None:
	# print('\x1b[2mquery: %s\x1b[m' % url)
	resp = requests.get(url)
	if resp.status_code != HTTPStatus.OK:
		return None
	return resp.json()


__recent_searches:dict = {}

def search(search, type='series', year=None):

	# /search/tv

	if not _api_key:
		raise NoAPIKey()

	path = 'search'
	if type == 'series':
		path += '/tv'
	else:
		path += '/movie'

	query = {
		'query': search,
	}
	if year is not None:
		query['first_air_date_year'] = year

	url = _qurl(path, query)

	if url in __recent_searches:
		return __recent_searches.get(url)

	data = _query(url)
	if not data:
		return []

	hits = data.get('results', [])

	_rename_keys(hits, {
		'name': 'title',
		'first_air_date': 'date',
		'original_name': 'original_title',
		'original_language': 'language',
		'origin_country': 'country',
	})
	_del_keys(hits, [
		'backdrop_path',
		'popularity',
		'poster_path',
		'vote_average',
		'vote_count',
		'genre_ids',   # for now (in this tool), we don't need these
	])
	_set_values(hits, {
		'year': lambda hit: [int(hit.get('date', [0]).split('-')[0])] if hit.get('date') else None,
		'id': lambda hit: str(hit['id']),
		'country': lambda hit: ', '.join(hit.get('country')),
	})
	_del_empty(hits)

	if builtins.type(hits) is dict:
		hits = [ hits ]

	__recent_searches[url] = hits

	return hits



__imdb_id_to_tmdb:dict = {}

def _get_tmdb_id(imdb_id):
	data = _query(_qurl('find/%s' % imdb_id, {'external_source':'imdb_id'}))
	if data is None or not data.get('tv_results'):
		raise RuntimeError('Unknown IMDb ID: %s' % imdb_id)

	series = data.get('tv_results', [])
	# "there can be only one"
	title_id = series[0]['id']
	__imdb_id_to_tmdb[imdb_id] = str(title_id)

	return title_id


__details:dict = {}

def details(title_id, type='series'):

	if not _api_key:
		raise NoAPIKey()

	if title_id.__class__ is list:
		return _parallel_query(details, map(lambda I: ( (I,), {} ) , title_id))

	if title_id.startswith('tt'):
		title_id = _get_tmdb_id(title_id)

	data = __details.get(title_id)
	if data is not None:
		return data

	detail_path = 'tv/%s' % title_id
	if type == 'film':
		detail_path = 'movie/%s' % title_id

	executor = __get_executor()
	detail_promise = executor.submit(_query, _qurl(detail_path))
	ext_promise = executor.submit(_query, _qurl('tv/%s/external_ids' % title_id))

	concurrent.futures.wait([ detail_promise, ext_promise ])

	data = detail_promise.result()
	if not data:
		#print('[tmdb] no details for %s' % title_id, file=sys.stderr)
		return None

	ext_id = ext_promise.result() or {}

	imdb_id = ext_id.get('imdb_id') or None
	if imdb_id:
		data['imdb_id'] = imdb_id

	_rename_keys(data, {
		'name': 'title',
		'first_air_date': 'date',
		'last_air_date': 'end_date',
		'original_name': 'original_title',
		'original_language': 'language',
		'origin_country': 'country',
		'number_of_seasons': 'total_seasons',
		'number_of_episodes': 'total_episodes',
	})
	_del_keys(data, [
		'backdrop_path',
		'popularity',
		'poster_path',
		'vote_average',
		'vote_count',
		'production_companies',
		'production_countries',
		'homepage',
		'in_production',
		'languages',
		'spoken_languages',
		'last_episode_to_air',
		'next_episode_to_air',
		'networks',
		'type',
		'tagline',
		'seasons',
		'created_by',
		'adult',
		'episode_run_time',
	])
	_set_values(data, {
		'year': lambda _: [int(data.get('date', [0]).split('-')[0])] if data.get('date') else None,
		'id': lambda _: str(data['id']),
		'country': lambda _: ', '.join(data.get('country')),
		'genre': lambda _: ', '.join(map(lambda g: g.get('name'), data.get('genres')))
	})
	_del_keys(data, ['genres'])

	# TODO: cast, crew?

	if data.get('status') in ('Ended', 'Canceled') and data.get('end_date'):
		data['year'] = data['year'] + [ int(data.get('end_date').split('-')[0]) ]
	else:
		del data['end_date']

	__details[title_id] = data

	return data


def episodes(series_id, with_details=False):

	if not _api_key:
		raise NoAPIKey()

	if series_id.startswith('tt'):
		series_id = _get_tmdb_id(series_id)


	# unfortunately we must synchronously get the details first
	ser_details = details(series_id, type='series')

	num_seasons = ser_details.get('total_seasons', 1)
	ep_runtime = ser_details.get('episode_run_time')

	def fetch_season(season):
		data = _query(_qurl('tv/%s/season/%d' % (series_id, season)))
		if data is None:
			return []

		episodes = data.get('episodes', [])

		_rename_keys(episodes, {
			'name': 'title',
			'first_air_date': 'date',
			'original_name': 'original_title',
			'original_language': 'language',
			'origin_country': 'country',
		})
		_del_keys(episodes, ['production_code', 'vote_average', 'vote_count'])
		_rename_keys(episodes, {
			'air_date': 'date',
			'season_number': 'season',
			'episode_number': 'episode',
		})
		_set_values(episodes, {
			'director': lambda ep: _job_persons(ep.get('crew', []), 'Director'),
			'writer': lambda ep: _job_persons(ep.get('crew', []), 'Writer'),
			'cast': lambda ep: ', '.join(map(lambda p: p.get('name'), ep.get('guest_stars', [])))
		})
		_del_keys(episodes, ['id', 'still_path', 'crew', 'guest_stars'])

		return episodes

	# then fetch all the seasons, in parallel
	executor = __get_executor()
	promises = [
		executor.submit(fetch_season, season)
		for season in range(1, num_seasons + 1)
	]

	concurrent.futures.wait(promises)

	episodes = [
		episode
		for promise in promises
		for episode in promise.result()
	]

	# if the series contains runtime info, populate each episode (unless already present)
	if ep_runtime:
		for ep in episodes:
			if not ep.get('runtime'):
				ep['runtime'] = ep_runtime

	if with_details:
		return ser_details, episodes

	return episodes


def changes(series_id:str|list, after:datetime, ignore:tuple=None) -> list:

	if _qurl is None:
		return []

	if series_id.__class__ is list:
		return _parallel_query(changes, map(lambda I: ( (I, after), {'ignore': ignore} ), series_id))

	now = datetime.now().date().isoformat()
	after_str = after.date().isoformat()

	data = _query(_qurl('tv/%s/changes' % series_id, {'start_date': after_str, 'end_date': now}))

	# remove change entries that was requested to ignore
	if data and data.get('changes') and isinstance(ignore, (tuple, list)):
		def non_ignored(entry):
			return entry.get('key') not in ignore
		data['changes'] = list(filter(non_ignored, data['changes']))

	return (data or {}).get('changes', [])


def _job_persons(people, job):
	return ', '.join(
		person.get('name')
		for person in people
		if person.get('job') == job
	)

def _del_empty(data):
	if type(data) is list:
		for item in data:
			_del_empty(item)

	elif type(data) is dict:
		for key, value in list(data.items()):
			if not value:
				del data[key]

def _del_keys(data, keys):
	if type(data) is list:
		for item in data:
			_del_keys(item, keys)

	elif type(data) is dict:
		for key in keys:
			data.pop(key, 0)

def _lower_case_keys(data):
	if type(data) is list:
		for item in data:
			_lower_case_keys(item)

	elif type(data) is dict:
		for key, value in list(data.items()):
			if type(key) is str:
				keyL = key.lower()
				if keyL != key:
					del data[key]
					data[keyL] = value

_missing = object()

def _rename_keys(data, renames):
	if type(data) is list:
		for item in data:
			_rename_keys(item, renames)

	elif type(data) is dict:
		for old, new in renames.items():
			value = data.pop(old, _missing)
			if value is not _missing:
				data[new] = value

def _set_values(data, new_values):
	if type(data) is list:
		for item in data:
			_set_values(item, new_values)

	elif type(data) is dict:
		for key, setter in new_values.items():
			try:
				value = setter(data)
				if value is not None:
					data[key] = value
				else:
					data.pop(key, None)
			except Exception as e:
				print('_set_values:', key, str(e), file=sys.stderr)

def _parallel_query(func, arg_list):
	executor = __get_executor()
	promises = [
		executor.submit(func, *args, **kw)
		for args, kw in arg_list
	]
	concurrent.futures.wait(promises)

	return [
		promise.result()
		for promise in promises
	]


def _self_test(args):
	def next(required=True):
		if required:
			return args.pop(0)
		else:
			return args.pop(0, None)

	op = next()

	if op.startswith('s'):
		info = search(next(), year=int(next()))

	elif op.startswith('e'):
		info = episodes(next())

	elif op.startswith('d'):
		info = details(next())

	elif op.startswith('c'):
		dt = datetime.now() - timedelta(days=1)
		info = changes(next(), after=dt, ignore=('images',))

	else:
		print('_self_test: <op> [args...]', file=sys.stderr)
		print('   <op> one of s(earch) e(pisodes) d(etails) c(hanges)', file=sys.stderr)

	print(json.dumps(info, indent=2))
	print('ENTRIES: %d' % len(info), file=sys.stderr)

if __name__ == '__main__':
	_self_test(sys.argv[1:])