from flask import Flask, request, jsonify, abort, render_template, redirect, session, url_for
import MySQLdb.cursors
import hashlib
import html
import json
import math
import os
import pathlib
import random
import re
import string
import urllib
import sys
from werkzeug.contrib.profiler import ProfilerMiddleware, MergeStream

static_folder = pathlib.Path(__file__).resolve().parent.parent / 'public'
app = Flask(__name__, static_folder=str(static_folder), static_url_path='')

app.secret_key = 'tonymoris'

f = open('/home/isucon/profiler.log', 'w')
stream = MergeStream(sys.stdout, f)
app.config['PROFILE'] = True
app.wsgi_app = ProfilerMiddleware(app.wsgi_app, stream, sort_by=('time', 'calls'))


keywords_cache = None
keyword_re_cache = None


# app.logger.critical('this is a CRITICAL message')


_config = {
    'db_host':       os.environ.get('ISUDA_DB_HOST', 'localhost'),
    'db_port':       int(os.environ.get('ISUDA_DB_PORT', '3306')),
    'db_user':       os.environ.get('ISUDA_DB_USER', 'root'),
    'db_password':   os.environ.get('ISUDA_DB_PASSWORD', ''),
    'isutar_origin': os.environ.get('ISUTAR_ORIGIN', 'http://localhost:5001'),
    'isupam_origin': os.environ.get('ISUPAM_ORIGIN', 'http://localhost:5050'),
}


def config(key):
    if key in _config:
        return _config[key]
    else:
        raise "config value of %s undefined" % key


def dbh_isuda():
    if hasattr(request, 'isuda_db'):
        return request.isuda_db
    else:
        request.isuda_db = MySQLdb.connect(**{
            'host': config('db_host'),
            'port': config('db_port'),
            'user': config('db_user'),
            'passwd': config('db_password'),
            'db': 'isuda',
            'charset': 'utf8mb4',
            'cursorclass': MySQLdb.cursors.DictCursor,
            'autocommit': True,
        })
        cur = request.isuda_db.cursor()
        cur.execute("SET SESSION sql_mode='TRADITIONAL,NO_AUTO_VALUE_ON_ZERO,ONLY_FULL_GROUP_BY'")
        cur.execute('SET NAMES utf8mb4')
        return request.isuda_db


def dbh_isutar():
    if hasattr(request, 'isutar_db'):
        return request.isutar_db
    else:
        request.isutar_db = MySQLdb.connect(**{
            'host': os.environ.get('ISUTAR_DB_HOST', 'localhost'),
            'port': int(os.environ.get('ISUTAR_DB_PORT', '3306')),
            'user': os.environ.get('ISUTAR_DB_USER', 'root'),
            'passwd': os.environ.get('ISUTAR_DB_PASSWORD', ''),
            'db': 'isutar',
            'charset': 'utf8mb4',
            'cursorclass': MySQLdb.cursors.DictCursor,
            'autocommit': True,
        })
        cur = request.isutar_db.cursor()
        cur.execute("SET SESSION sql_mode='TRADITIONAL,NO_AUTO_VALUE_ON_ZERO,ONLY_FULL_GROUP_BY'")
        cur.execute('SET NAMES utf8mb4')
        return request.isutar_db


@app.teardown_request
def close_db(exception=None):
    if hasattr(request, 'db'):
        request.db.close()


@app.template_filter()
def ucfirst(str):
    return str[0].upper() + str[-len(str) + 1:]


def set_name(func):
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" in session:
            request.user_id = user_id = session['user_id']
            cur = dbh_isuda().cursor()
            cur.execute('SELECT name FROM user WHERE id = %s', (user_id, ))
            user = cur.fetchone()
            if user is None:
                abort(403)
            request.user_name = user['name']

        return func(*args, **kwargs)
    return wrapper


def authenticate(func):
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not hasattr(request, 'user_id'):
            abort(403)
        return func(*args, **kwargs)

    return wrapper


@app.route('/initialize')
def get_initialize():
    global keywords_cache
    global keyword_re_cache
    keywords_cache = None
    keyword_re_cache = None

    cur = dbh_isuda().cursor()
    cur.execute('DELETE FROM entry WHERE id > 7101')
    origin = config('isutar_origin')
    urllib.request.urlopen(origin + '/initialize')
    return jsonify(result='ok')


@app.route('/')
@set_name
def get_index():
    PER_PAGE = 10
    page = int(request.args.get('page', '1'))

    cur = dbh_isuda().cursor()
    cur.execute('SELECT * FROM entry ORDER BY updated_at DESC LIMIT %s OFFSET %s', (PER_PAGE, PER_PAGE * (page - 1),))
    entries = cur.fetchall()
    for entry in entries:
        entry['html'] = htmlify(entry['description'])
        entry['stars'] = load_stars(entry['keyword'])

    cur.execute('SELECT COUNT(*) AS count FROM entry')
    row = cur.fetchone()
    total_entries = row['count']
    last_page = int(math.ceil(total_entries / PER_PAGE))
    pages = range(max(1, page - 5), min(last_page, page + 5) + 1)

    return render_template('index.html', entries=entries, page=page, last_page=last_page, pages=pages)


@app.route('/robots.txt')
def get_robot_txt():
    abort(404)


@app.route('/keyword', methods=['POST'])
@set_name
@authenticate
def create_keyword():
    global keywords_cache
    global keyword_re_cache
    keyword = request.form['keyword']
    if keyword is None or len(keyword) == 0:
        abort(400)
    if keywords_cache is not None:
        keywords_cache.add(keyword)
    keyword_re_cache = None

    user_id = request.user_id
    description = request.form['description']

    if is_spam_contents(description) or is_spam_contents(keyword):
        abort(400)

    cur = dbh_isuda().cursor()
    sql = """
        INSERT INTO entry (author_id, keyword, description, created_at, updated_at)
        VALUES (%s,%s,%s,NOW(), NOW())
        ON DUPLICATE KEY UPDATE
        author_id = %s, keyword = %s, description = %s, updated_at = NOW()
"""
    cur.execute(sql, (user_id, keyword, description, user_id, keyword, description))
    return redirect('/')


@app.route('/register')
@set_name
def get_register():
    return render_template('authenticate.html', action='register')


@app.route('/register', methods=['POST'])
def post_register():
    name = request.form['name']
    pw = request.form['password']
    if name is None or name == '' or pw is None or pw == '':
        abort(400)

    user_id = register(dbh_isuda().cursor(), name, pw)
    session['user_id'] = user_id
    return redirect('/')


def register(cur, user, password):
    salt = random_string(20)
    cur.execute("INSERT INTO user (name, salt, password, created_at) VALUES (%s, %s, %s, NOW())",
                (user, salt, hashlib.sha1((salt + "password").encode('utf-8')).hexdigest(),))
    cur.execute("SELECT LAST_INSERT_ID() AS last_insert_id")
    return cur.fetchone()['last_insert_id']


def random_string(n):
    return ''.join([random.choice(string.ascii_letters + string.digits) for i in range(n)])


@app.route('/login')
@set_name
def get_login():
    return render_template('authenticate.html', action='login')


@app.route('/login', methods=['POST'])
def post_login():
    name = request.form['name']
    cur = dbh_isuda().cursor()
    cur.execute("SELECT * FROM user WHERE name = %s", (name, ))
    row = cur.fetchone()
    if row is None or row['password'] != hashlib.sha1((row['salt'] + request.form['password']).encode('utf-8')).hexdigest():
        abort(403)

    session['user_id'] = row['id']
    return redirect('/')


@app.route('/logout')
def get_logout():
    session.pop('user_id', None)
    return redirect('/')


@app.route('/keyword/<keyword>')
@set_name
def get_keyword(keyword):
    if keyword == '':
        abort(400)

    cur = dbh_isuda().cursor()
    cur.execute('SELECT * FROM entry WHERE keyword = %s', (keyword,))
    entry = cur.fetchone()
    if entry is None:
        abort(404)

    entry['html'] = htmlify(entry['description'])
    entry['stars'] = load_stars(entry['keyword'])
    return render_template('keyword.html', entry=entry)


@app.route('/keyword/<keyword>', methods=['POST'])
@set_name
@authenticate
def delete_keyword(keyword):
    global keywords_cache
    global keyword_re_cache
    if keyword == '':
        abort(400)
    if keywords_cache is not None and keyword in keywords_cache:
        keywords_cache.remove(keyword)
    keyword_re_cache = None

    cur = dbh_isuda().cursor()
    cur.execute('SELECT keyword FROM entry WHERE keyword = %s', (keyword, ))
    row = cur.fetchone()
    if row is None:
        abort(404)

    cur.execute('DELETE FROM entry WHERE keyword = %s', (keyword,))

    return redirect('/')


def make_keyword_list():
    global keywords_cache
    if keywords_cache is not None:
        return list(keywords_cache)

    cur = dbh_isuda().cursor()
    cur.execute('SELECT keyword FROM entry ORDER BY CHARACTER_LENGTH(keyword) DESC')
    keywords = list()
    for k in cur.fetchall():
        keywords.append(k['keyword'])
    keywords_cache = set(keywords)
    return keywords


def make_keyword_re(keywords):
    global keyword_re_cache
    if keyword_re_cache is not None:
        return keyword_re_cache
    keyword_re_cache = re.compile("(%s)" % '|'.join([re.escape(k) for k in keywords]))
    return keyword_re_cache


def htmlify(content):
    if content is None or content == '':
        return ''

    # cur = dbh_isuda().cursor()
    # cur.execute('SELECT * FROM entry ORDER BY CHARACTER_LENGTH(keyword) DESC')
    # keywords = cur.fetchall()
    keywords = make_keyword_list()
    keyword_re = make_keyword_re(keywords)
    kw2sha = {}

    def replace_keyword(m):
        kw2sha[m.group(0)] = "isuda_%s" % hashlib.sha1(m.group(0).encode('utf-8')).hexdigest()
        return kw2sha[m.group(0)]

    result = re.sub(keyword_re, replace_keyword, content)
    result = html.escape(result)
    for kw, hash in kw2sha.items():
        url = url_for('get_keyword', keyword=kw)
        link = "<a href=\"%s\">%s</a>" % (url, html.escape(kw))
        result = re.sub(re.compile(hash), link, result)

    return re.sub(re.compile("\n"), "<br />", result)


def get_stars(keyword):
    cur = dbh_isutar().cursor()
    app.logger.critical('keyword = ' + keyword)
    cur.execute('SELECT * FROM star WHERE keyword = %s', (keyword, ))
    res = cur.fetchall()
    return res


def load_stars(keyword):
    # cur = dbh_isutar().cursor()
    # cur.execute('SELECT * FROM star WHERE keyword = %s', (keyword, ))
    # res = cur.fetchall()
    # return res

    origin = config('isutar_origin')
    url = "%s/stars" % origin
    params = urllib.parse.urlencode({'keyword': keyword})
    with urllib.request.urlopen(url + "?%s" % params) as res:
        data = json.loads(res.read().decode('utf-8'))
        return data['stars']


def is_spam_contents(content):
    with urllib.request.urlopen(config('isupam_origin'), urllib.parse.urlencode({"content": content}).encode('utf-8')) as res:
        data = json.loads(res.read().decode('utf-8'))
        return not data['valid']

    return False


if __name__ == "__main__":
    app.run()
