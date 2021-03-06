try: import simplejson as json
except ImportError: import json
import re
import operator
import urllib, urllib2
import datetime
import warnings
import logging
import httplib
import contextlib
import urlparse
import xml.sax.saxutils as saxutils


from pytube.stream import Stream, YtData
from pytube.utils import yt_ts_to_datetime
import pytube.exceptions


class Category(str):
    """ A simple str subclass;
        by subclassing we can attach attributes to instances.
    """
    SCHEME = u'http://gdata.youtube.com/schemas/2007/categories.cat'


class LinksMixin(object):
    """ Provides parsing of strangely formatted youtube api links objects
    """
    def _parse_links(self, links):
        self._links = {}
        for link in links:
            body = link.copy()
            del body[u'rel']
            name = link[u'rel']
            if name.startswith('http://gdata.youtube.com/schemas/2007#'):
                name = name[len('http://gdata.youtube.com/schemas/2007#'):]
            self._links[name] = body

        # check to see if we can set up some useful references
        if 'video.related' in self._links:
            self.related_videos = VideoStream(self.client, self._links['video.related'][u'href'])
        if 'video.responses' in self._links:
            self.video_responses = VideoStream(self.client, self._links['video.responses'][u'href'])
        if 'insight.views' in self._links:
            self.insight_url = self._links['insight.views']['href']
        if 'edit' in self._links:
            self.edit_url = self._links['edit'][u'href']


class Profile(YtData, LinksMixin):
    """ Collects data about a YouTube user/channel. """

    def __init__(self, client, data):
        assert data[u'version'] == u'1.0', "Youtube API version mismatch"
        self.client = client
        entry = data[u'entry']

        self.id = entry[u'yt$username'][u'$t']
        self.api_id = entry[u'id'][u'$t']
        self.thumbnail = entry[u'media$thumbnail'][u'url']
        self.title = entry[u'title'][u'$t']
        self.updated = yt_ts_to_datetime(entry[u'updated'][u'$t'])
        self.author = {
            'name': entry[u'author'][0][u'name'][u'$t'],
            'username': entry[u'yt$username'][u'$t'],
            'age': entry[u'yt$age'][u'$t'],
            'location': entry[u'yt$location'][u'$t'],
        }
        if u'yt$gender' in entry:
            self.author['gender'] = entry[u'yt$gender'][u'$t']

        self.statistics = entry[u'yt$statistics'].copy()
        self.statistics[u'lastWebAccess'] = yt_ts_to_datetime(self.statistics[u'lastWebAccess'])
        self.statistics[u'subscriberCount'] = int(self.statistics[u'subscriberCount'])
        self.statistics[u'totalUploadViews'] = int(self.statistics[u'totalUploadViews'])
        self.statistics[u'videoWatchCount'] = int(self.statistics[u'videoWatchCount'])
        self.statistics[u'viewCount'] = int(self.statistics[u'viewCount'])

        self._parse_feeds(entry['gd$feedLink'])
        self._parse_links(entry['link'])

    def subscribe(self):
        self.client.subscribe(self.author['username'])

    def subscriptions(self):
        return self.client.user_subscriptions(self.author['username'])

    def __repr__(self):
        return "<YouTube Profile: %s>" % (str(self.id),)

    def __unicode__(self):
        return u"<YouTube Profile: %s>" % (str(self.id),)


class Video(YtData, LinksMixin):
    """ Collects data about a YouTube Video. """

    EDIT_URL = "http://gdata.youtube.com/feeds/api/users/%(user_id)s/uploads/%(video_id)s"

    def _parse_categories(self, data):
        """ Given category data from the youtube API, parse it into the
            category and keyword attributes on self.
        """
        # parse the category
        categories = [c for c in data if c['scheme'] == Category.SCHEME]
        assert len(categories) == 1
        self.category = Category(categories[0]['term'])
        self.category.label = categories[0]['label']

        # parse keywords
        keyword_scheme = u'http://gdata.youtube.com/schemas/2007/keywords.cat'
        keywords = [kw for kw in data if kw['scheme'] == keyword_scheme]
        self.keywords = [kw['term'] for kw in keywords]
        return

    def _init_jsonc(self, data):
        # missing insight_url and private fields when compared to json resposne
        self.id = data['id']
        self.title = data['title']
        self.author = data['uploader']
        self.category = data['category']
        self.description = data['description']
        self.comment_count = int(data['commentCount'])
        self.comments = self.client.video_comments(self.id)
        self.duration = int(data['duration'])
        self.favorite_count = int(data['favoriteCount'])
        self.like_count = int(data['likeCount'])
        self.updated = yt_ts_to_datetime(data['updated'])
        self.uploaded = yt_ts_to_datetime(data['uploaded'])
        self.published = self.uploaded
        self.view_count = int(data['viewCount'])
        self.aspect_ratio = data['aspectRatio']
        self.keywords = data['tags']
        self.dislike_count = int(data['ratingCount']) - self.like_count
        self.private = False # Not returned by jsonc for now

    def _init_json(self, data):
        self._parse_links(data[u'link'])
        self._parse_categories(data[u'category'])
        self.title = data[u'title'][u'$t']
        self.author = data[u'author'][0][u'name'][u'$t']
        self.api_id = data[u'id']['$t']

        try:
            self.id = data[u'media$group'][u'yt$videoid'][u'$t']
        except KeyError:
            assert data[u'id'][u'$t'].startswith('http://gdata.youtube.com/feeds/api/videos/')
            assert len(data[u'id'][u'$t']) == 53
            self.id = data[u'id'][u'$t'][-11:]

        self.updated = yt_ts_to_datetime(data[u'updated'][u'$t'])
        if u'published' in data: # Not given to us by playlists
            self.published = yt_ts_to_datetime(data[u'published'][u'$t'])
        else:
            self.published = self.updated # just default to updated date for now

        if u'yt$rating' in data:
            self.like_count = int(data[u'yt$rating'][u'numLikes'])
            self.dislike_count = int(data[u'yt$rating'][u'numDislikes'])

        self.access_control = dict((d[u'action'], d[u'permission']) for d in data[u'yt$accessControl'])
        self.comments = self.client.video_comments(self.id)

        # All the following attributes don't exist for certain restricted videos
        if u'media$description' in data[u'media$group']:
            self.description = data[u'media$group'][u'media$description'][u'$t']
        if u'yt$uploaded' in data[u'media$group']:
            self.uploaded = yt_ts_to_datetime(data[u'media$group'][u'yt$uploaded'][u'$t'])
        if u'yt$duration' in data[u'media$group']:
            self.duration = int(data[u'media$group'][u'yt$duration'][u'seconds'])
        if u'yt$aspectRatio' in data[u'media$group']:
            self.aspect_ratio = data[u'media$group'][u'yt$aspectRatio'][u'$t']
        if u'yt$statistics' in data:
            self.favorite_count = int(data[u'yt$statistics'][u'favoriteCount'])
            self.view_count = int(data[u'yt$statistics'][u'viewCount'])
        if u'gd$comments' in data:
            self.comment_count = int(data[u'gd$comments'][u'gd$feedLink'][u'countHint'])
            self.comments._count = self.comment_count

        if u'yt$private' in data[u'media$group']:
            self.private = True
        else:
            self.private = False

    def __init__(self, client, data, data_format='json'):
        self.client = client

        if data_format == 'jsonc':
            self._init_jsonc(data)
        else:
            self._init_json(data)

    def __repr__(self):
        return "<YouTube Video: %s>" % (str(self.id),)

    def __unicode__(self):
        return u"<YouTube Video: %s>" % (str(self.id),)

    def respond_to(self, video_id):
        self.client.video_response(self.id, video_id)

    def update(self, timeout=None):
        """ Updates this video's metadata on youtube
        """
        timeout = timeout or self.client.default_timeout
        xml_template = """<?xml version="1.0"?>
<entry xmlns="http://www.w3.org/2005/Atom"
  xmlns:media="http://search.yahoo.com/mrss/"
  xmlns:yt="http://gdata.youtube.com/schemas/2007">
  <media:group>
    <media:title type="plain">{title}</media:title>
    <media:description type="plain">{description}</media:description>
    <media:category scheme="http://gdata.youtube.com/schemas/2007/categories.cat">{category}</media:category>
    <media:keywords>{keywords}</media:keywords>
    {private}
  </media:group>
{accessControl}
</entry>
        """

        def format_acl_row(a,p):
            template = """  <yt:accessControl action="{a}" permission="{p}"/>"""
            return template.format(a=a,p=p)

        def format_acl():
            return '\n'.join(
                format_acl_row(k,v) for k,v in self.access_control.items()
            )

        params = {
            'title': saxutils.escape(self.title).encode('utf-8'),
            'description': saxutils.escape(self.description).encode('utf-8'),
            'category': self.category,
            'keywords': ','.join(saxutils.escape(k).encode('utf-8') for k in self.keywords),
            'accessControl': format_acl(),
            'private': """<yt:private/>""" if self.private else ''
        }

        request_body = xml_template.format(**params)

        # get the url
        edit_url = getattr(self, 'edit_url', None)
        if not edit_url:
            edit_url = self.EDIT_URL % {
                'user_id': self.author,
                'video_id': self.id,
            }
        url = urlparse.urlparse(edit_url)

        headers = self.client._default_headers()
        headers['GData-Version'] = 2
        headers['Content-Type'] = 'application/atom+xml'

        # urllib2 doesn't support the PUT method.
        # time to use httplib instead.
        with contextlib.closing(
            httplib.HTTPConnection(url.netloc, timeout=timeout)
            ) as connection:
            connection.request("PUT", url.path, request_body, headers)
            response = connection.getresponse()
            response_body = response.read()
        if response.status != 200:
            data = {
                'url': edit_url,
                'request_body': request_body,
                'headers': headers,
                'response': response,
                'response_body': response_body
            }
            msg = 'Response Status: %s\n%s' % (response.status, response_body)
            e = pytube.exceptions.VideoUpdateException(msg, data)
            raise e
        return

class VideoStream(Stream, LinksMixin):
    """ Stream for parsing YouTube Video results """

    def _handle_data(self, data):
        assert data[u'version'] == u'1.0', "Youtube API version mismatch"
        self._count = int(data[u'feed'][u'openSearch$totalResults'][u'$t'])
        self.title = data[u'feed'][u'title'][u'$t']
        self.updated = yt_ts_to_datetime(data[u'feed'][u'updated'][u'$t'])
        self._parse_links(data[u'feed'][u'link'])
        videos = data['feed'].get('entry', ())
        return [Video(self.client, x) for x in videos]

    def __repr__(self):
        return "<YouTube VideoStream: %s>" % (self.uri,)

    def __unicode__(self):
        return u"<YouTube VideoStream: %s>" % (self.uri,)


class SubscriptionStream(Stream):
    """ Stream for parsing YouTube Subscription results """

    def _handle_data(self, data):
        assert data[u'version'] == u'1.0', "Youtube API version mismatch"
        self._count = int(data[u'feed'][u'openSearch$totalResults'][u'$t'])
        subscriptions = data['feed'].get('entry', ())
        return [subscription['yt$username']['$t'] for subscription in subscriptions]

    def __repr__(self):
        return "<YouTube Subscriptions: %s>" % (self.uri,)

    def __unicode__(self):
        return u"<YouTube VideoStream: %s>" % (self.uri,)


class Comment(object):
    """ Transforms YouTube API response into a usable comment object with
        native datatypes.
    """
    def __init__(self, data):
        self.id = data[u'id'][u'$t']
        self.author = data[u'author'][0][u'name'][u'$t']
        self.title = data[u'title'][u'$t']
        self.content = data[u'content'][u'$t']
        self.published = yt_ts_to_datetime(data[u'published'][u'$t'])
        self.updated = yt_ts_to_datetime(data[u'updated'][u'$t'])


class CommentStream(Stream, LinksMixin):
    """ Stream for parsing YouTube Comment results """
    def _handle_data(self, data):
        assert data[u'version'] == u'1.0', "Youtube API version mismatch"
        self._count = int(data[u'feed'][u'openSearch$totalResults'][u'$t'])
        self.title = data[u'feed'][u'title'][u'$t']
        self.updated = yt_ts_to_datetime(data[u'feed'][u'updated'][u'$t'])
        self._parse_links(data[u'feed'][u'link'])
        return [Comment(d) for d in data['feed']['entry']]


class PlaylistEntry(object):
    def __init__(self, client, playlist_id, entry_data):
        self.id = entry_data[u'id'][u'$t'].split(':')[-1]
        self.api_id = entry_data[u'id'][u'$t']
        self.position = int(entry_data[u'yt$position'][u'$t'])
        self.playlist_id = playlist_id

        vid = Video(client, entry_data)
        self.video = vid

        # replace api_id since it'll be the playlist entry api_id and not the video one
        vid.api_id = vid.api_id[:vid.api_id.find('playlist')] + 'video:' + vid.id

    def __str__(self):
        return '<PlaylistEntry %s: %s (%s)' % (self.position, self.id, self.video.id)

    def __repr__(self):
        return self.__str__()

    def __unicode__(self):
        return self.__str__()


class Playlist(object):
    ADD_VIDEO_URL = "http://gdata.youtube.com/feeds/api/playlists/%(playlist_id)s?v=2&alt=json"
    EDIT_VIDEO_URL = "http://gdata.youtube.com/feeds/api/playlists/%(playlist_id)s/%(playlist_entry_id)s"

    def __init__(self, client, data):
        assert data[u'version'] == u'1.0', "Youtube API version mismatch"
        data = data['feed']

        self.client = client
        self.id = data[u'yt$playlistId'][u'$t']
        self.author = data[u'author'][0][u'name'][u'$t']
        self.title = data[u'media$group'][u'media$description'][u'$t']
        self.description = data[u'media$group'][u'media$title'][u'$t']
        self.updated = yt_ts_to_datetime(data[u'updated'][u'$t'])

        self._handle_videos(data)

    def _handle_videos(self, data):
        """
        Loads each video into a PlaylistEntry and returns them listed in order
        """
        self.entries = [PlaylistEntry(self.client, self.id, entry) for entry in data[u'entry']]
        self.entries = sorted(self.entries, key=lambda entry: entry.position)

    def remove_entry(self, entry_id, timeout=None):
        timeout = timeout or self.client.default_timeout
        edit_video_url = self.EDIT_VIDEO_URL % {'playlist_id': self.id, 'playlist_entry_id': entry_id}
        json_response = self.client._gdata_jsonc(edit_video_url, 'DELETE')

        # Remove deleted entry and update entry positions
        if json_response['status'] == 200:
            pos_count = 1
            del_pos = 0

            for i in range(len(self.entries)):
                if self.entries[i].id == entry_id:
                    del_pos = i
                else:
                    self.entries[i].position = pos_count
                    pos_count += 1
            del self.entries[del_pos]

    def remove_video(self, video_id, timeout=None):
        for entry in self.entries:
            if entry.video.id == video_id:
                self.remove_entry(entry.id, timeout)
                break;

    def add_video(self, video_id, timeout=None):
        timeout = timeout or self.client.default_timeout
        add_video_url = self.ADD_VIDEO_URL % {'playlist_id': self.id}
        request_body = """<?xml version="1.0" encoding="UTF-8"?>
<entry xmlns="http://www.w3.org/2005/Atom"
    xmlns:yt="http://gdata.youtube.com/schemas/2007">
  <id>%s</id>
</entry>""" % video_id
        url = urlparse.urlparse(add_video_url)
        headers = self.client._default_headers()
        headers['GData-Version'] = 2
        headers['Content-Type'] = 'application/atom+xml'

        with contextlib.closing(
            httplib.HTTPConnection(url.netloc, timeout=timeout)
            ) as connection:
            connection.request("POST", '%s?%s' % (url.path, url.query), request_body, headers)
            response = connection.getresponse()
            response_body = response.read()

        if response.status != 201:
            data = {
                'url': add_video_url,
                'request_body': request_body,
                'headers': headers,
                'response': response,
                'response_body': response_body
            }
            msg = 'Response Status: %s\n%s' % (response.status, response_body)
            e = pytube.exceptions.PlaylistException(msg, data)
            raise e

        entry_data = json.loads(response_body)
        self.entries.append(PlaylistEntry(self.client, self.id, entry_data['entry']))


class Client(object):
    """ The YouTube API Client

        You must provide an app identifier to use the youtube API.
        You may also provide a developer API key (http://code.google.com/apis/youtube/dashboard/)
        which will be submitted with all API requests.
    """

    GOOGLE_AUTH_URL = 'https://www.google.com/accounts/ClientLogin'
    YOUTUBE_SEARCH_URL = 'http://gdata.youtube.com/feeds/api/videos'
    YOUTUBE_VIDEO_URL = 'http://gdata.youtube.com/feeds/api/videos/%(video_id)s'
    YOUTUBE_PLAYLIST_URL = 'https://gdata.youtube.com/feeds/api/playlists/%(playlist_id)s'
    YOUTUBE_PROFILE_URL = 'http://gdata.youtube.com/feeds/api/users/%(username)s'
    YOUTUBE_UPLOADS_URL = 'http://gdata.youtube.com/feeds/api/users/%(username)s/uploads'
    YOUTUBE_COMMENTS_URL = 'http://gdata.youtube.com/feeds/api/videos/%(video_id)s/comments'
    YOUTUBE_SUBSCRIBE_URL = 'http://gdata.youtube.com/feeds/api/users/default/subscriptions'
    YOUTUBE_SUBSCRIPTIONS_URL = 'http://gdata.youtube.com/feeds/api/users/%(username)s/subscriptions?alt=json&v=2'
    YOUTUBE_RESPONSE_URL = 'http://gdata.youtube.com/feeds/api/videos/%(original_video_id)s/responses'

    def __init__(self, app_name, dev_key=None):
        self._auth_data = None
        self.username = None
        self.default_timeout = None
        self.app_name = app_name
        self.dev_key = dev_key

    def _default_headers(self):
        """ Headers that should be added to all gdata requests
        """
        dh = self._auth_headers()
        if self.dev_key:
            dh['X-GData-Key'] = 'key=' + self.dev_key
        return dh

    def _auth_headers(self):
        """ Generate any GData authorization headers
        """
        if self._auth_data is None:
            return {}
        if 'Auth' in self._auth_data:
            return {
                'Authorization': "GoogleLogin auth=" + self._auth_data['Auth'],
            }
        if 'authsub_token' in self._auth_data:
            return {
                'Authorization': "AuthSub token=" + self._auth_data['authsub_token'],
            }
        return {}


    def _http_request(self, url, method='GET', request_body='', params={}, headers={}, timeout=None):
        parsed_url = urlparse.urlparse(url)
        params = urllib.urlencode(params)
        request_url = parsed_url.path

        if method == 'POST':
            request_body += params
            if parsed_url.query != '':
                request_url =  '%s?%s' % (request_url, parsed_url.query)
        elif method == 'GET':
            request_url = '%s?%s' % (request_url, params)
            request_url += parsed_url.query

        with contextlib.closing( # Just ensures we close the connection no matter what
            httplib.HTTPConnection(parsed_url.netloc, timeout=timeout)
        ) as connection:
            connection.request(method, request_url, request_body, headers)
            response = connection.getresponse()
            return (response.status, response.read())

        return None

    def _gdata_jsonc(self, url, method='GET', request_body='', params={}, headers={}, timeout=None):
        headers.update({
            'Content-Type': 'application/json',
            'GData-Version': 2 # jsonc requires v2
        })
        headers.update(self._default_headers())

        status, response = self._http_request(url, method, request_body, params, headers, timeout)
        json_response = {'status': status, 'response': response, 'data': {}}

        if (status == 200 and method == 'GET') or status == 201:
            json_response['data'] = json.loads(response)['data']
        elif status in (401, 403): # Handle these errors here, but pass on rest
            if 'TokenExpired' in response:
                raise pytube.exceptions.TokenExpired(response)
            raise pytube.exceptions.AuthenticationError(response)

        return json_response

    def _gdata_request(self, url, query=None, data=None, headers=None, timeout=None):
        timeout = timeout or self.default_timeout

        if query:
            sep = '?' if '?' not in url else '&'
            url += sep + urllib.urlencode(query)

        headers = headers or {}
        headers.update(self._default_headers())

        request = urllib2.Request(url, data, headers)
        try:
            return urllib2.urlopen(request, timeout=timeout)
        except urllib2.HTTPError, e:
            if e.getcode() == 401:
                e.response = e.read()
                if 'TokenExpired' in e.response:
                    raise pytube.exceptions.TokenExpired()
                raise e
            raise

    def _gdata_json(self, url, query=None, data=None, headers=None, timeout=None):
        query = query or {}
        query.update({'alt': 'json'})
        return json.load(
            self._gdata_request(
                url,
                query=query,
                data=data,
                headers=headers,
                timeout=timeout
            )
        )

    def _auth_headers(self):
        if self._auth_data is None:
            return {}
        if 'Auth' in self._auth_data:
            return {
                'Authorization': "GoogleLogin auth=" + self._auth_data['Auth'],
            }
        if 'authsub_token' in self._auth_data:
            return {
                'Authorization': "AuthSub token=" + self._auth_data['authsub_token'],
            }
        return {}

    def _client_login(self, username, password, captcha=None):
        """ Try to login with gdata ClientLogin"""
        auth_data = {
            'Email': username,
            'Passwd': password,
            'service': 'youtube',
            'source': self.app_name,
        }
        if captcha:
            auth_data.update({
                'logintoken': captcha.token,
                'logincaptcha': captcha.solved,
            })
        auth_data = urllib.urlencode(auth_data)
        try:
            response = self._gdata_request(
                self.GOOGLE_AUTH_URL,
                query=None,
                data=auth_data
            )
        except urllib2.HTTPError, e:
            # convert the response into a usable error dict
            response = e.read()
            data = dict([r.split('=', 1) for r in response.strip().split()])
            # we just trashed the response iterator; put the response back in
            # an attribute on the exception that the caller can read.
            e.response = response

            if e.getcode() == 403:
                errors = {
                    'BadAuthentication': "Invalid Credentials",
                    "AccountDisabled": "Account Disabled",
                }
                reason = errors.get(data.get('Error', None), None)
                if reason is not None:
                    raise pytube.exceptions.AuthenticationError(reason)
                if data.get('Error', None) == 'CaptchaRequired':
                    raise pytube.exceptions.CaptchaRequired('Captcha Required', data)
            raise

        self._auth_data = dict([r.split('=') for r in response.read().split()])
        self.username = username

    def _authsub_login(self, token):
        """Authenticates this user with an authsub token"""
        self._auth_data = {
            'authsub_token': token,
        }

    def authenticate(self, username=None, password=None, captcha=None, authsub=None):
        """ Authenticates this client with YouTube.

            You may provide either a username and password, which will invoke
            the gdata ClientLogin, or you may pass an authsub token.
        """
        assert (username and password) or authsub
        if username and password:
            self._client_login(username, password, captcha)
        elif authsub:
            self._authsub_login()

    def unauthenticate(self):
        """ Unauthenticates this client.

            This does not invalidate any login token that was generated; there
            does not seem to be an API to do that. We simply delete local
            references to the token.
        """
        self._auth_data = None
        self.username = None

    def user_profile(self, username='default'):
        """ Gets username's youtube profile. If authenticated, may be called without
            passing a username to get your own profile.
        """
        data = self._gdata_json(self.YOUTUBE_PROFILE_URL % {'username': username })
        return Profile(self, data)

    def user_videos(self, username='default'):
        """ Gets a user's uploaded video stream. If authenticated, may be
            called without passing a username to get your own videos.
        """
        return VideoStream(self, self.YOUTUBE_UPLOADS_URL % {'username': username })

    def user_subscriptions(self, username='default'):
        """ Gets YouTube channel ids that username is following. If
            authenticated, may be called without passing a username to get
            your own subscriptions.
        """
        return SubscriptionStream(self, self.YOUTUBE_SUBSCRIPTIONS_URL % {'username': username })

    def video(self, video_id):
        """ Gets a specific video from the youtube API.
        """
        try:
            data = self._gdata_json(self.YOUTUBE_VIDEO_URL % {'video_id': video_id}, {'v': 2})
        except urllib2.HTTPError, e:
            if e.code == 403:
                e.response = e.read()
                if 'too_many_recent_calls' in e.response:
                    raise pytube.exceptions.QuotaException
                raise pytube.exceptions.PrivateVideoException
            if e.code == 404:
                raise pytube.exceptions.NoSuchVideoException
            raise
        return Video(self, data[u'entry'])

    def video_search(self, q=None, **query):
        """ Searches YouTube for videos matching a search term
        """
        query['q'] = q
        return VideoStream(self, self.YOUTUBE_SEARCH_URL, query=query)

    def video_comments(self, video_id):
        """ Gets Comments for a specific video
        """
        return CommentStream(self, self.YOUTUBE_COMMENTS_URL % {'video_id': video_id})

    def video_responses(self, video_id):
        return VideoStream(self, self.YOUTUBE_RESPONSE_URL % {'original_video_id': video_id})

    def subscribe(self, username='default'):
        """Subscribes the authenticated user to username's channels
        """
        assert self._auth_data is not None, "You must be authenticated to subscribe"
        subscribe_data = \
        '''<?xml version="1.0" encoding="UTF-8"?>
        <entry xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://gdata.youtube.com/schemas/2007">
            <category scheme="http://gdata.youtube.com/schemas/2007/subscriptiontypes.cat"
              term="channel"/>
            <yt:username>{0}</yt:username>
        </entry>'''.format(username)
        subscribe_headers = { 'Content-Type': 'application/atom+xml'}
        response = self._gdata_request(self.YOUTUBE_SUBSCRIBE_URL, None, subscribe_data, subscribe_headers)

    def video_response(self, original_video_id, response_video_id):
        response_data = \
        '''<?xml version="1.0" encoding="UTF-8"?>
        <entry xmlns="http://www.w3.org/2005/Atom">
          <id>{0}</id>
        </entry>'''.format(response_video_id)
        response_headers = { 'Content-Type': 'application/atom+xml'}
        response = self._gdata_request(self.YOUTUBE_RESPONSE_URL % {'original_video_id': original_video_id }, None, response_data, response_headers)

    def playlist(self, playlist_id):
        try:
            data = self._gdata_json(self.YOUTUBE_PLAYLIST_URL % {'playlist_id': playlist_id}, {'v': 2})
        except urllib2.HTTPError, e:
            raise
        return Playlist(self, data)

