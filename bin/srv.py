#!/usr/bin/env python
# vim: set encoding=utf-8

import gevent
from gevent.wsgi import WSGIServer
from gevent.queue import Queue
from gevent.monkey import patch_all
from gevent.subprocess import Popen, PIPE, STDOUT
patch_all()

import sys
import logging
import os
import re
import requests
import socket
import time
import json

import geoip2.database
import jinja2

from flask import Flask, request, render_template, send_from_directory, send_file, make_response
app = Flask(__name__)

MYDIR = os.path.abspath(
    os.path.dirname(os.path.dirname('__file__')))
sys.path.append("%s/lib/" % MYDIR)
import wttrin_png, parse_query
from translations import get_message, FULL_TRANSLATION, PARTIAL_TRANSLATION, SUPPORTED_LANGS
from buttons import TWITTER_BUTTON, GITHUB_BUTTON, GITHUB_BUTTON_2, GITHUB_BUTTON_3, GITHUB_BUTTON_FOOTER

from globals import GEOLITE, \
                    IP2LCACHE, ALIASES, BLACKLIST, \
                    get_help_file, BASH_FUNCTION_FILE, TRANSLATION_FILE, LOG_FILE, TEST_FILE, \
                    TEMPLATES, STATIC, \
                    NOT_FOUND_LOCATION, \
                    MALFORMED_RESPONSE_HTML_PAGE, \
                    IATA_CODES_FILE, \
                    log, error, \
                    LISTEN_PORT, LISTEN_HOST, PLAIN_TEXT_AGENTS

from wttr import get_wetter, get_moon

if not os.path.exists(os.path.dirname(LOG_FILE)):
    os.makedirs(os.path.dirname(LOG_FILE))
logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG, format='%(asctime)s %(message)s')

my_loader = jinja2.ChoiceLoader([
    app.jinja_loader,
    jinja2.FileSystemLoader(TEMPLATES),
])
app.jinja_loader = my_loader

class Limits:
    def __init__(self):
        self.intervals = ['min', 'hour', 'day']
        self.divisor = {
            'min':      60,
            'hour':     3600,
            'day':      86400,
            }
        self.counter = {
            'min':      {},
            'hour':     {},
            'day':      {},
            }
        self.limit = {
            'min':      30,
            'hour':     600,
            'day':      1000,
            }
        self.last_update = {
            'min':      0,
            'hour':     0,
            'day':      0,
            }
        self.clear_counters()

    def check_ip(self, ip_address):
        """
        check if connections from `ip_address` are allowed
        and raise a RuntimeError exception if they are not
        """
        if ip_address == '5.9.243.177':
            return
        self.clear_counters()
        for interval in self.intervals:
            if ip_address not in self.counter[interval]:
                self.counter[interval][ip_address] = 0
            self.counter[interval][ip_address] += 1
            if self.limit[interval] <= self.counter[interval][ip_address]:
                log("Too many queries: %s in %s for %s"
                    % (self.limit[interval], interval, ip_address))
                raise RuntimeError(
                    "Not so fast! Number of queries per %s is limited to %s"
                    % (interval, self.limit[interval]))

    def clear_counters( self ):
        t = int( time.time() )
        for interval in self.intervals:
            if t / self.divisor[interval] != self.last_update[interval]:
                self.counter[interval] = {}
                self.last_update[interval] = t / self.divisor[interval]

limits = Limits()

def is_ip(ip):
    if re.match('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', ip) is None:
        return False
    try:
        socket.inet_aton(ip)
        return True
    except socket.error:
        return False

def location_normalize(location):
    #translation_table = dict.fromkeys(map(ord, '!@#$*;'), None)
    def remove_chars( c, s ):
        return ''.join(x for x in s if x not in c )

    location = location.lower().replace('_', ' ').replace('+', ' ').strip()
    if not location.startswith('moon@'):
        location = remove_chars(r'!@#$*;:\\', location)
    return location

def load_aliases(aliases_filename):
    aliases_db = {}
    with open(aliases_filename, 'r') as f:
        for line in f.readlines():
            from_, to_ = line.decode('utf-8').split(':', 1)
            aliases_db[location_normalize(from_)] = location_normalize(to_)
    return aliases_db

def load_iata_codes(iata_codes_filename):
    with open(iata_codes_filename, 'r') as f:
        result = []
        for line in f.readlines():
            result.append(line.strip())
    return set(result)

location_alias = load_aliases(ALIASES)
location_black_list = [x.strip() for x in open(BLACKLIST, 'r').readlines()]
iata_codes = load_iata_codes(IATA_CODES_FILE)
print "IATA CODES LOADED: %s" % len(iata_codes)

def location_canonical_name(location):
    location = location_normalize(location)
    if location in location_alias:
        return location_alias[location.lower()]
    return location

def ascii_only(string):
    try:
        for i in range(5):
            string = string.encode('utf-8')
        return True
    except:
        return False

def geolocator(location):
    try:
        geo = requests.get('http://localhost:8004/%s' % location).text
    except Exception as e:
        print "ERROR: %s" % e
        return

    if geo == "":
        return

    try:
        answer = json.loads(geo.encode('utf-8'))
        return answer
    except Exception as e:
        print "ERROR: %s" % e
        return None

def ip2location(ip):
    cached = os.path.join(IP2LCACHE, ip)
    if not os.path.exists(IP2LCACHE):
        os.makedirs(IP2LCACHE)

    if os.path.exists(cached):
        location = open(cached, 'r').read()
        return location

    try:
        t = requests.get( 'http://api.ip2location.com/?ip=%s&key=%s&package=WS10' % (IP2LOCATION_KEY, ip)).text
        if ';' in t:
            location = t.split(';')[3]
            open(cached, 'w').write(location)
            print "ip2location says: %s" % location
            return location
    except:
            pass

reader = geoip2.database.Reader(GEOLITE)
def get_location(ip_addr):
    response = reader.city(ip_addr)

    country = response.country.iso_code
    city = response.city.name

    #
    # temporary disabled it because of geoip services capcacity
    #
    #if city is None and response.location:
    #    coord = "%s, %s" % (response.location.latitude, response.location.longitude)
    #    try:
    #        location = geolocator.reverse(coord, language='en')
    #        city = location.raw.get('address', {}).get('city')
    #    except:
    #        pass
    if city is None:
        city = ip2location(ip_addr)
    return (city or NOT_FOUND_LOCATION), country

def parse_accept_language(acceptLanguage):
  languages = acceptLanguage.split(",")
  locale_q_pairs = []

  for language in languages:
    try:
        if language.split(";")[0] == language:
          # no q => q = 1
          locale_q_pairs.append((language.strip(), "1"))
        else:
          locale = language.split(";")[0].strip()
          q = language.split(";")[1].split("=")[1]
          locale_q_pairs.append((locale, q))
    except:
        pass

  return locale_q_pairs

def find_supported_language(accepted_languages):
    for p in accepted_languages:
        lang = p[0]
        if '-' in lang:
            lang = lang.split('-',1)[0]
        if lang in SUPPORTED_LANGS:
            return lang
    return None

def show_help(location, lang):
    text = ""
    if location == ":help":
        text = open(get_help_file(lang), 'r').read()
        text = text.replace('FULL_TRANSLATION', ' '.join(FULL_TRANSLATION))
        text = text.replace('PARTIAL_TRANSLATION', ' '.join(PARTIAL_TRANSLATION))
    elif location == ":bash.function":
        text = open(BASH_FUNCTION_FILE, 'r').read()
    elif location == ":translation":
        text = open(TRANSLATION_FILE, 'r').read()
        text = text.replace('NUMBER_OF_LANGUAGES', str(len(SUPPORTED_LANGS))).replace('SUPPORTED_LANGUAGES', ' '.join(SUPPORTED_LANGS))
    return text.decode('utf-8')
show_help.pages = [':help', ':bash.function', ':translation' ]

@app.route('/files/<path:path>')
def send_static(path):
    return send_from_directory(STATIC, path)

@app.route('/favicon.ico')
def send_favicon():
    return send_from_directory(STATIC, 'favicon.ico')

@app.route('/malformed-response.html')
def send_malformed():
    return send_from_directory(STATIC, 'malformed-response.html')

@app.route("/")
@app.route("/<string:location>")
def wttr(location = None):
    """
    Main rendering function, it processes incoming weather queries.
    Depending on user agent it returns output in HTML or ANSI format.

    Incoming data:
        request.args
        request.headers
        request.remote_addr
        request.referrer
        request.query_string
    """
    if request.referrer:
        print request.referrer

    hostname = request.headers['Host']
    lang = None
    if hostname != 'wttr.in' and hostname.endswith('.wttr.in'):
        lang = hostname[:-8]

    if request.headers.getlist("X-Forwarded-For"):
       ip = request.headers.getlist("X-Forwarded-For")[0]
       if ip.startswith('::ffff:'):
           ip = ip[7:]
    else:
       ip = request.remote_addr

    try:
        limits.check_ip(ip)
    except RuntimeError, e:
        return str(e)
    except Exception, e:
        logging.error("Exception has occured", exc_info=1)
        return "ERROR"

    if location is not None and location.lower() in location_black_list:
        return ""

    png_filename = None
    if location is not None and location.lower().endswith(".png"):
        png_filename = location
        location = location[:-4]

    query = parse_query.parse_query(request.args)

    if 'lang' in request.args:
        lang = request.args.get('lang')
    if lang is None and 'Accept-Language' in request.headers:
        lang = find_supported_language(parse_accept_language(request.headers.get('Accept-Language', '')))

    user_agent = request.headers.get('User-Agent', '').lower()

    html_output = not any(agent in user_agent for agent in PLAIN_TEXT_AGENTS)


    if location in show_help.pages:
        help_ = show_help(location, lang)
        if html_output:
            return render_template('index.html', body=help_)
        else:
            return help_

    orig_location = location

    if request.headers.getlist("X-Forwarded-For"):
        ip = request.headers.getlist("X-Forwarded-For")[0]
        if ip.startswith('::ffff:'):
            ip = ip[7:]
    else:
        ip = request.remote_addr

    try:
        # if location is starting with ~
        # or has non ascii symbols
        # it should be handled like a search term (for geolocator)
        override_location_name = None
        full_address = None

        if location is not None and not ascii_only(location):
            location = "~" + location

        if location is not None and location.upper() in iata_codes:
            location = '~%s' % location

        if location is not None and location.startswith('~'):
            geolocation = geolocator(location_canonical_name(location[1:]))
            if geolocation is not None:
                override_location_name = location[1:].replace('+', ' ')
                location = "%s,%s" % (geolocation['latitude'], geolocation['longitude'])
                full_address = geolocation['address']
                print full_address
            else:
                location = NOT_FOUND_LOCATION #location[1:]
        try:
            query_source_location = get_location(ip)
        except:
            query_source_location = NOT_FOUND_LOCATION, None

        # what units should be used
        # metric or imperial
        # based on query and location source (imperial for US by default)
        print "lang = %s" % lang
        if query.get('use_metric', False) and not query.get('use_imperial', False):
            query['use_imperial'] = False
            query['use_metric'] = True
        elif query.get('use_imperial', False) and not query.get('use_metric', False):
            query['use_imperial'] = True
            query['use_metric'] = False
        elif lang == 'us':
            # slack uses m by default, to override it speciy us.wttr.in
            query['use_imperial'] = True
            query['use_metric'] = False
        else:
            if query_source_location[1] in ['US'] and 'slack' not in user_agent:
                query['use_imperial'] = True
                query['use_metric'] = False
            else:
                query['use_imperial'] = False
                query['use_metric'] = True

        country = None
        if location is None or location == 'MyLocation':
            location, country = query_source_location

        if is_ip(location):
            location, country = get_location(location)
        if location.startswith('@'):
            try:
                location, country = get_location(socket.gethostbyname(location[1:]))
            except:
                query_source_location = NOT_FOUND_LOCATION, None

        location = location_canonical_name(location)
        log("%s %s %s %s %s %s" \
            % (ip, user_agent, orig_location, location, query.get('use_imperial', False), lang))

        # We are ready to return the answer
        if png_filename:
            options = {}
            if lang is not None:
                options['lang'] = lang

            options['location'] = "%s,%s" % (location, country)
            options.update(query)

            cached_png_file = wttrin_png.make_wttr_in_png(png_filename, options=options)
            response = make_response(send_file(cached_png_file,
                                               attachment_filename=png_filename,
                                               mimetype='image/png'))

            # Trying to disable github caching
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

        if location == 'moon' or location.startswith('moon@'):
            output = get_moon(location, html=html_output, lang=lang)
        else:
            if country and location != NOT_FOUND_LOCATION:
                location = "%s, %s" % (location, country)
            output = get_wetter(location, ip,
                                html=html_output,
                                lang=lang,
                                query=query,
                                location_name=override_location_name,
                                full_address=full_address,
                                url=request.url,
                               )

        if 'Malformed response' in str(output) \
                or 'API key has reached calls per day allowed limit' in str(output):
            if html_output:
                return MALFORMED_RESPONSE_HTML_PAGE
            return get_message('CAPACITY_LIMIT_REACHED', lang).encode('utf-8')

        if html_output:
            output = output.replace('</body>',
                                    (TWITTER_BUTTON
                                     + GITHUB_BUTTON
                                     + GITHUB_BUTTON_3
                                     + GITHUB_BUTTON_2
                                     + GITHUB_BUTTON_FOOTER) + '</body>')
        else:
            if query.get('days', '3') != '0':
                #output += '\n' + get_message('NEW_FEATURE', lang).encode('utf-8')
                output += '\n' + get_message('FOLLOW_ME', lang).encode('utf-8') + '\n'
        return output

    #except RuntimeError, e:
    #    return str(e)
    except Exception, e:
        if 'Malformed response' in str(e) \
                or 'API key has reached calls per day allowed limit' in str(e):
            if html_output:
                return MALFORMED_RESPONSE_HTML_PAGE
            return get_message('CAPACITY_LIMIT_REACHED', lang).encode('utf-8')
        logging.error("Exception has occured", exc_info=1)
        return "ERROR"

server = WSGIServer((LISTEN_HOST, LISTEN_PORT), app)
server.serve_forever()
