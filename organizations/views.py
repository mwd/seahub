# encoding: utf-8
import sys
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.core.mail import send_mail
from django.contrib.sites.models import Site, RequestSite
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render_to_response
from django.template import Context, loader, RequestContext

from auth.decorators import login_required
from pysearpc import SearpcError
from seaserv import ccnet_threaded_rpc, get_orgs_by_user, get_org_repos, \
    get_org_by_url_prefix, create_org, get_user_current_org, add_org_user, \
    get_ccnetuser, remove_org_user, get_org_groups, is_valid_filename

from forms import OrgCreateForm
from signals import org_user_added
from settings import ORG_CACHE_PREFIX
from utils import set_org_ctx
from notifications.models import UserNotification
from registration.models import RegistrationProfile
import seahub.settings as seahub_settings
from seahub.utils import go_error, go_permission_error, validate_group_name, \
    emails2list, gen_token
from seahub.views import myhome

@login_required
def create_org(request):
    """
    Create org account.
    """
    if request.method == 'POST':
        form = OrgCreateForm(request.POST)
        if form.is_valid():
            org_name = form.cleaned_data['org_name']
            url_prefix = form.cleaned_data['url_prefix']
            username = request.user.username
            
            try:
                # create_org(org_name, url_prefix, username)
                ccnet_threaded_rpc.create_org(org_name, url_prefix, username)
                return HttpResponseRedirect(\
                    reverse(org_info, args=[url_prefix]))
            except SearpcError, e:
                return go_error(request, e.msg)
            
    else:
        form = OrgCreateForm()

    return render_to_response('organizations/create_org.html', {
            'form': form,
            }, context_instance=RequestContext(request))

@login_required
def org_info(request, url_prefix):
    """
    Show org info page.
    """
    org = get_user_current_org(request.user.username, url_prefix)
    if not org:
        return HttpResponseRedirect(reverse(myhome))

    set_org_ctx(request, org._dict)
    
    org_members = ccnet_threaded_rpc.get_org_emailusers(url_prefix,
                                                        0, sys.maxint)
    repos = get_org_repos(org.org_id, 0, sys.maxint)

    url = 'organizations/%s/repo/create/' % org.url_prefix
    return render_to_response('organizations/org_info.html', {
            'org': org,
            'org_users': org_members,
            'repos': repos,
            'url': seahub_settings.SITE_ROOT + url,
            }, context_instance=RequestContext(request))

@login_required
def org_groups(request, url_prefix):
    """
    List org groups and add org group.
    """
    org = get_user_current_org(request.user.username, url_prefix)
    if not org:
        return HttpResponseRedirect(reverse(myhome))

    if request.method == 'POST':
        group_name = request.POST.get('group_name')
        if not validate_group_name(group_name):
            return go_error(request, u'小组名称只能包含中英文字符，数字及下划线')
        
        try:
            group_id = ccnet_threaded_rpc.create_group(group_name.encode('utf-8'),
                                   request.user.username)
            ccnet_threaded_rpc.add_org_group(org.org_id, group_id)
        except SearpcError, e:
            error_msg = e.msg
            return go_error(request, error_msg)
        
    groups = get_org_groups(org.org_id, 0, sys.maxint)
    return render_to_response('organizations/org_groups.html', {
            'org': org,
            'groups': groups,
            }, context_instance=RequestContext(request))

def send_org_user_add_mail(request, email, password, org_name):
    """
    Send email when add new user.
    """
    use_https = request.is_secure()
    domain = RequestSite(request).domain
    
    t = loader.get_template('organizations/org_user_add_email.html')
    c = {
        'user': request.user.username,
        'org_name': org_name,
        'email': email,
        'password': password,
        'domain': domain,
        'protocol': use_https and 'https' or 'http',
        }
    
    try:
        send_mail(u'SeaCloud注册信息', t.render(Context(c)),
                  None, [email], fail_silently=False)
        messages.add_message(request, messages.INFO, email)
    except:
        messages.add_message(request, messages.ERROR, email)
    
@login_required
def org_useradmin(request, url_prefix):
    """
    List and add org users.
    """
    if not request.user.org['is_staff']:
        raise Http404

    if request.method == 'POST':
        emails = request.POST.get('added-member-name')

        email_list = emails2list(emails)
        for email in email_list:
            if not email or email.find('@') <= 0 :
                continue
            
            org_id = request.user.org['org_id']
            if get_ccnetuser(username=email):
                email = email.strip(' ')
                org_id = request.user.org['org_id']
                add_org_user(org_id, email, 0)
                
                # send signal
                org_user_added.send(sender=None, org_id=org_id,
                                    from_email=request.user.username,
                                    to_email=email)
            else:
                # User is not registered, just create account and
                # add that account to org
                password = gen_token(max_length=6)
                if Site._meta.installed:
                    site = Site.objects.get_current()
                else:
                    site = RequestSite(request)
                RegistrationProfile.objects.create_active_user(\
                    email, email, password, site, send_email=False)
                add_org_user(org_id, email, 0)
                if hasattr(seahub_settings, 'EMAIL_HOST'):
                    org_name = request.user.org['org_name']
                    send_org_user_add_mail(request, email, password, org_name)

    # Make sure page request is an int. If not, deliver first page.
    try:
        current_page = int(request.GET.get('page', '1'))
        per_page= int(request.GET.get('per_page', '25'))
    except ValueError:
        current_page = 1
        per_page = 25

    url_prefix = request.user.org['url_prefix']
    users_plus_one = ccnet_threaded_rpc.get_org_emailusers(\
        url_prefix, per_page * (current_page - 1), per_page + 1)
    if len(users_plus_one) == per_page + 1:
        page_next = True
    else:
        page_next = False
        
    users = users_plus_one[:per_page]
    for user in users:
        if user.props.id == request.user.id:
            user.is_self = True
            
    return render_to_response(
        'organizations/org_useradmin.html', {
            'users': users,
            'current_page': current_page,
            'prev_page': current_page-1,
            'next_page': current_page+1,
            'per_page': per_page,
            'page_next': page_next,
        },
        context_instance=RequestContext(request))

@login_required
def org_user_remove(request, user):
    """
    Remove org user
    """
    org_id = request.user.org['org_id']
    url_prefix = request.user.org['url_prefix']
    remove_org_user(org_id, user)

    return HttpResponseRedirect(reverse('org_useradmin', args=[url_prefix]))

def org_msg(request):
    """
    Show organization user added messages
    """
    orgmsg_list = []
    notes = UserNotification.objects.filter(to_user=request.user.username)
    for n in notes:
        if n.msg_type == 'org_msg':
            orgmsg_list.append(n.detail)

    # remove new org msg notification
    UserNotification.objects.filter(to_user=request.user.username,
                                    msg_type='org_msg').delete()

    return render_to_response('organizations/new_msg.html', {
            'orgmsg_list': orgmsg_list,
            }, context_instance=RequestContext(request))

@login_required    
def org_repo_create(request):
    '''
    Handle ajax post to create org repo.
    
    '''
    if request.method != 'POST':
        return Http404
    repo_name = request.POST.get("repo_name")
    repo_desc = request.POST.get("repo_desc")
    encrypted = int(request.POST.get("encryption"))
    passwd = request.POST.get("passwd")
    passwd_again = request.POST.get("passwd_again")

    result = {}
    content_type = 'application/json; charset=utf-8'

    error_msg = ""
    if not repo_name:
        error_msg = u"目录名不能为空"
    elif len(repo_name) > 50:
        error_msg = u"目录名太长"
    elif not is_valid_filename(repo_name):
        error_msg = (u"您输入的目录名 %s 包含非法字符" % repo_name)
    elif not repo_desc:
        error_msg = u"描述不能为空"
    elif len(repo_desc) > 100:
        error_msg = u"描述太长"
    elif encrypted == 1:
        if not passwd:
            error_msg = u"密码不能为空"
        elif not passwd_again:
            error_msg = u"确认密码不能为空"
        elif len(passwd) < 3:
            error_msg = u"密码太短"
        elif len(passwd) > 15:
            error_msg = u"密码太长"
        elif passwd != passwd_again:
            error_msg = u"两次输入的密码不相同"

    if error_msg:
        result['error'] = error_msg
        return HttpResponse(json.dumps(result), content_type=content_type)

    try:
        user = request.user.username
        org_id = request.user.org['org_id']
        repo_id = create_org_repo(repo_name, repo_desc, user, passwd, org_id)
        result['success'] = True
    except:
        result['error'] = u"创建目录失败"
    else:
        if not repo_id:
            result['error'] = u"创建目录失败"
            
    return HttpResponse(json.dumps(result), content_type=content_type)
