#!/usr/bin/env python
# encoding: utf-8
import settings
import re
import time
import os
import stat
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils.hashcompat import sha_constructor

from django.core.files.uploadhandler import FileUploadHandler, StopFutureHandlers, StopUpload
from django.core.cache import cache

from seaserv import seafserv_rpc, ccnet_threaded_rpc, seafserv_threaded_rpc, \
    get_repo, get_commits, get_group_repoids

EMPTY_SHA1 = '0000000000000000000000000000000000000000'

def go_permission_error(request, msg=None):
    """
    Return permisson error page.

    """
    return render_to_response('permission_error.html', {
            'error_msg': msg or u'权限错误',
            }, context_instance=RequestContext(request))

def go_error(request, msg=None):
    """
    Return normal error page.

    """
    return render_to_response('error.html', {
            'error_msg': msg or u'内部错误',
            }, context_instance=RequestContext(request))

def list_to_string(l):
    """
    Return string of a list.

    """
    return ','.join(l)

def get_httpserver_root():
    """
    Get seafile http server address and port from settings.py,
    and cut out last '/'.

    """
    if settings.HTTP_SERVER_ROOT[-1] == '/':
        http_server_root = settings.HTTP_SERVER_ROOT[:-1]
    else:
        http_server_root = settings.HTTP_SERVER_ROOT
    return http_server_root

def get_ccnetapplet_root():
    """
    Get ccnet applet address and port from settings.py,
    and cut out last '/'.

    """
    if settings.CCNET_APPLET_ROOT[-1] == '/':
        ccnet_applet_root = settings.CCNET_APPLET_ROOT[:-1]
    else:
        ccnet_applet_root = settings.CCNET_APPLET_ROOT
    return ccnet_applet_root

def gen_token():
    """
    Generate short token used for owner to access repo file.

    """

    token = sha_constructor(settings.SECRET_KEY + unicode(time.time())).hexdigest()[:5]
    return token

def validate_group_name(group_name):
    """
    Check whether group name is valid.
    A valid group name only contains alphanumeric character.

    """
    return re.match('^\w+$', group_name, re.U)

def calculate_repo_last_modify(repo_list):
    """
    Get last modify time for repo. 
    
    """
    for repo in repo_list:
        try:
            repo.latest_modify = get_commits(repo.id, 0, 1)[0].ctime
        except:
            repo.latest_modify = None

class UploadProgressCachedHandler(FileUploadHandler):
    """Tracks progress for file uploads. The http post request must contain a
    header or query parameter, 'X-Progress-ID', which should contain a unique
    string to identify the upload to be tracked.

    """

    def __init__(self, request=None):
        super(UploadProgressCachedHandler, self).__init__(request)
        self.progress_id = None
        self.cache_key = None
        self.chunk_size = 1024

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.content_length = content_length
        if 'X-Progress-ID' in self.request.GET :
            self.progress_id = self.request.GET['X-Progress-ID']
        elif 'X-Progress-ID' in self.request.META:
            self.progress_id = self.request.META['X-Progress-ID']
        print "handle_raw_input: ", self.progress_id
        if self.progress_id:
            self.cache_key = "%s_%s" % (self.request.user.username, self.progress_id )
            # make cache expiring in 30 seconds
            cache.set(self.cache_key, {
                'length': self.content_length,
                'uploaded' : 0
            }, 30)            

    def new_file(self, field_name, file_name, content_type, content_length, charset=None):
        pass

    def receive_data_chunk(self, raw_data, start):
        time.sleep(1)
        if self.cache_key:
            data = cache.get(self.cache_key)
            data['uploaded'] += self.chunk_size
            cache.set(self.cache_key, data, 30)
        return raw_data
    
    def file_complete(self, file_size):
        pass

    def upload_complete(self):
        if self.cache_key:
            cache.delete(self.cache_key)

def check_filename_with_rename(repo_id, parent_dir, filename):
    latest_commit = get_commits(repo_id, 0, 1)[0]
    dirents = seafserv_rpc.list_dir_by_path(latest_commit.id,
                                         parent_dir.encode('utf-8'))

    def no_duplicate(name):
        for dirent in dirents:
            if dirent.obj_name == name:
                return False
        return True

    def make_new_name(filename, i):
        base, ext = os.path.splitext(filename)
        if ext:
            new_base = "%s (%d)" % (base, i)
            return new_base + ext
        else:
            return "%s (%d)" % (filename, i)

    if no_duplicate(filename):
        return filename
    else:
        i = 1
        while True:
            new_name = make_new_name (filename, i)
            if no_duplicate(new_name):
                return new_name
            else:
                i += 1


def get_accessible_repos(request, repo):
    """Get all repos the current user can access when coping/moving files
    online. If the repo is encrypted, then files can only be copied/moved
    within the same repo. Otherwise, files can be copied/moved between
    owned/shared/group repos of the current user.

    """
    def check_has_subdir(repo):
        latest_commit = get_commits(repo.props.id, 0, 1)[0]
        if latest_commit.root_id == EMPTY_SHA1:
            return False

        dirs = seafserv_rpc.list_dir_by_path(latest_commit.id, '/')

        for dirent in dirs:
            if stat.S_ISDIR(dirent.props.mode):
                return True

    if repo.encrypted:
        repo.has_subdir = check_has_subdir(repo)
        accessible_repos = [repo]
        return accessible_repos

    accessible_repos = []

    email = request.user.username
    owned_repos = seafserv_threaded_rpc.list_owned_repos(email)
    shared_repos = seafserv_threaded_rpc.list_share_repos(email, 'to_email', -1, -1)
    groups_repos = []
    groups = ccnet_threaded_rpc.get_groups(email)
    for group in groups:
        group_repo_ids = get_group_repoids(group_id=group.id)
        for repo_id in group_repo_ids:
            if not repo_id:
                continue
            group_repo = get_repo(repo_id)
            if not group_repo:
                continue
            group_repo.share_from = seafserv_threaded_rpc.get_group_repo_share_from(repo_id)
            if email != group_repo.share_from:
                groups_repos.append(group_repo)

    def has_repo(repos, repo):
        for r in repos:
            if repo.id == r.id:
                return True
        return False

    for repo in owned_repos + shared_repos + groups_repos:
        if not has_repo(accessible_repos, repo):
            if not repo.props.encrypted:
                accessible_repos.append(repo)
                repo.props.has_subdir = check_has_subdir(repo)

    return accessible_repos
