##############################################################################
#
# Copyright (c) 2002 Zope Foundation and Contributors.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Access control package.
"""

import os
from base64 import decodestring

from Acquisition import aq_base
from Acquisition import aq_parent
from Acquisition import Implicit
from Persistence import Persistent
from Persistence import PersistentMapping
from zExceptions import BadRequest
from zExceptions import Unauthorized
from zope.interface import implements

# TODO dependencies
from App.Management import Navigation
from App.Management import Tabs
from App.special_dtml import DTMLFile
from App.Dialogs import MessageDialog
from OFS.role import RoleManager
from OFS.SimpleItem import Item

from AccessControl import AuthEncoding
from AccessControl import ClassSecurityInfo
from AccessControl.class_init import InitializeClass
from AccessControl.interfaces import IStandardUserFolder
from AccessControl.Permissions import manage_users as ManageUsers
from AccessControl.requestmethod import requestmethod
from AccessControl.rolemanager import DEFAULTMAXLISTUSERS
from AccessControl.SecurityManagement import getSecurityManager
from AccessControl.SecurityManagement import newSecurityManager
from AccessControl.SecurityManagement import noSecurityManager
from AccessControl.users import User
from AccessControl.users import readUserAccessFile
from AccessControl.users import _remote_user_mode
from AccessControl.users import emergency_user
from AccessControl.users import nobody
from AccessControl.users import addr_match
from AccessControl.users import host_match
from AccessControl.users import reqattr
from AccessControl.ZopeSecurityPolicy import _noroles


class BasicUserFolder(Implicit, Persistent, Navigation, Tabs, RoleManager,
                      Item):
    """Base class for UserFolder-like objects"""

    meta_type='User Folder'
    id       ='acl_users'
    title    ='User Folder'

    isPrincipiaFolderish=1
    isAUserFolder=1
    maxlistusers = DEFAULTMAXLISTUSERS

    encrypt_passwords = 1

    security = ClassSecurityInfo()

    manage_options=(
        (
        {'label': 'Contents', 'action': 'manage_main'},
        {'label': 'Properties', 'action':'manage_userFolderProperties'},
        )
        +RoleManager.manage_options
        +Item.manage_options
        )

    # ----------------------------------
    # Public UserFolder object interface
    # ----------------------------------

    security.declareProtected(ManageUsers, 'getUserNames')
    def getUserNames(self):
        """Return a list of usernames"""
        raise NotImplementedError

    security.declareProtected(ManageUsers, 'getUsers')
    def getUsers(self):
        """Return a list of user objects"""
        raise NotImplementedError

    security.declareProtected(ManageUsers, 'getUser')
    def getUser(self, name):
        """Return the named user object or None"""
        raise NotImplementedError

    security.declareProtected(ManageUsers, 'getUserById')
    def getUserById(self, id, default=None):
        """Return the user corresponding to the given id.
        """
        # The connection between getting by ID and by name is not a strong
        # one
        user = self.getUser(id)
        if user is None:
            return default
        return user

    def _doAddUser(self, name, password, roles, domains, **kw):
        """Create a new user. This should be implemented by subclasses to
           do the actual adding of a user. The 'password' will be the
           original input password, unencrypted. The implementation of this
           method is responsible for performing any needed encryption."""
        raise NotImplementedError

    def _doChangeUser(self, name, password, roles, domains, **kw):
        """Modify an existing user. This should be implemented by subclasses
           to make the actual changes to a user. The 'password' will be the
           original input password, unencrypted. The implementation of this
           method is responsible for performing any needed encryption."""
        raise NotImplementedError

    def _doDelUsers(self, names):
        """Delete one or more users. This should be implemented by subclasses
           to do the actual deleting of users."""
        raise NotImplementedError

    # As of Zope 2.5, userFolderAddUser, userFolderEditUser and
    # userFolderDelUsers offer aliases for the the _doAddUser, _doChangeUser
    # and _doDelUsers methods (with the difference that they can be called
    # from XML-RPC or untrusted scripting code, given the necessary
    # permissions).
    #
    # Authors of custom user folders don't need to do anything special to
    # support these - they will just call the appropriate '_' methods that
    # user folder subclasses already implement.

    security.declareProtected(ManageUsers, 'userFolderAddUser')
    @requestmethod('POST')
    def userFolderAddUser(self, name, password, roles, domains,
                          REQUEST=None, **kw):
        """API method for creating a new user object. Note that not all
           user folder implementations support dynamic creation of user
           objects."""
        if hasattr(self, '_doAddUser'):
            return self._doAddUser(name, password, roles, domains, **kw)
        raise NotImplementedError

    security.declareProtected(ManageUsers, 'userFolderEditUser')
    @requestmethod('POST')
    def userFolderEditUser(self, name, password, roles, domains,
                           REQUEST=None, **kw):
        """API method for changing user object attributes. Note that not
           all user folder implementations support changing of user object
           attributes."""
        if hasattr(self, '_doChangeUser'):
            return self._doChangeUser(name, password, roles, domains, **kw)
        raise NotImplementedError

    security.declareProtected(ManageUsers, 'userFolderDelUsers')
    @requestmethod('POST')
    def userFolderDelUsers(self, names, REQUEST=None):
        """API method for deleting one or more user objects. Note that not
           all user folder implementations support deletion of user objects."""
        if hasattr(self, '_doDelUsers'):
            return self._doDelUsers(names)
        raise NotImplementedError


    # -----------------------------------
    # Private UserFolder object interface
    # -----------------------------------

    _remote_user_mode=_remote_user_mode
    _domain_auth_mode=0
    _emergency_user=emergency_user
    # Note: use of the '_super' name is deprecated.
    _super=emergency_user
    _nobody=nobody


    def identify(self, auth):
        if auth and auth.lower().startswith('basic '):
            try: name, password=tuple(decodestring(
                                      auth.split(' ')[-1]).split(':', 1))
            except:
                raise BadRequest, 'Invalid authentication token'
            return name, password
        else:
            return None, None

    def authenticate(self, name, password, request):
        emergency = self._emergency_user
        if name is None:
            return None
        if emergency and name==emergency.getUserName():
            user = emergency
        else:
            user = self.getUser(name)
        if user is not None and user.authenticate(password, request):
            return user
        else:
            return None

    def authorize(self, user, accessed, container, name, value, roles):
        user = getattr(user, 'aq_base', user).__of__(self)
        newSecurityManager(None, user)
        security = getSecurityManager()
        try:
            try:
                # This is evil: we cannot pass _noroles directly because
                # it is a special marker, and that special marker is not
                # the same between the C and Python policy implementations.
                # We __really__ need to stop using this marker pattern!
                if roles is _noroles:
                    if security.validate(accessed, container, name, value):
                        return 1
                else:
                    if security.validate(accessed, container, name, value,
                                         roles):
                        return 1
            except:
                noSecurityManager()
                raise
        except Unauthorized: pass
        return 0

    def validate(self, request, auth='', roles=_noroles):
        """
        this method performs identification, authentication, and
        authorization
        v is the object (value) we're validating access to
        n is the name used to access the object
        a is the object the object was accessed through
        c is the physical container of the object

        We allow the publishing machinery to defer to higher-level user
        folders or to raise an unauthorized by returning None from this
        method.
        """
        v = request['PUBLISHED'] # the published object
        a, c, n, v = self._getobcontext(v, request)

        # we need to continue to support this silly mode
        # where if there is no auth info, but if a user in our
        # database has no password and he has domain restrictions,
        # return him as the authorized user.
        if not auth:
            if self._domain_auth_mode:
                for user in self.getUsers():
                    if user.getDomains():
                        if self.authenticate(user.getUserName(), '', request):
                            if self.authorize(user, a, c, n, v, roles):
                                return user.__of__(self)

        name, password = self.identify(auth)
        user = self.authenticate(name, password, request)
        # user will be None if we can't authenticate him or if we can't find
        # his username in this user database.
        emergency = self._emergency_user
        if emergency and user is emergency:
            if self._isTop():
                # we do not need to authorize the emergency user against the
                # published object.
                return emergency.__of__(self)
            else:
                # we're not the top-level user folder
                return None
        elif user is None:
            # either we didn't find the username, or the user's password
            # was incorrect.  try to authorize and return the anonymous user.
            if self._isTop() and self.authorize(self._nobody, a,c,n,v,roles):
                return self._nobody.__of__(self)
            else:
                # anonymous can't authorize or we're not top-level user folder
                return None
        else:
            # We found a user, his password was correct, and the user
            # wasn't the emergency user.  We need to authorize the user
            # against the published object.
            if self.authorize(user, a, c, n, v, roles):
                return user.__of__(self)
            # That didn't work.  Try to authorize the anonymous user.
            elif self._isTop() and self.authorize(self._nobody,a,c,n,v,roles):
                return self._nobody.__of__(self)
            else:
                # we can't authorize the user, and we either can't authorize
                # nobody against the published object or we're not top-level
                return None

    if _remote_user_mode:

        def validate(self, request, auth='', roles=_noroles):
            v = request['PUBLISHED']
            a, c, n, v = self._getobcontext(v, request)
            name = request.environ.get('REMOTE_USER', None)
            if name is None:
                if self._domain_auth_mode:
                    for user in self.getUsers():
                        if user.getDomains():
                            if self.authenticate(
                                user.getUserName(), '', request
                                ):
                                if self.authorize(user, a, c, n, v, roles):
                                    return user.__of__(self)

            user = self.getUser(name)
            # user will be None if we can't find his username in this user
            # database.
            emergency = self._emergency_user
            if emergency and name==emergency.getUserName():
                if self._isTop():
                    # we do not need to authorize the emergency user against
                    #the published object.
                    return emergency.__of__(self)
                else:
                    # we're not the top-level user folder
                    return None
            elif user is None:
                # we didn't find the username in this database
                # try to authorize and return the anonymous user.
                if self._isTop() and self.authorize(self._nobody,
                                                    a, c, n, v, roles):
                    return self._nobody.__of__(self)
                else:
                    # anonymous can't authorize or we're not top-level user
                    # folder
                    return None
            else:
                # We found a user and the user wasn't the emergency user.
                # We need to authorize the user against the published object.
                if self.authorize(user, a, c, n, v, roles):
                    return user.__of__(self)
                # That didn't work.  Try to authorize the anonymous user.
                elif self._isTop() and self.authorize(
                    self._nobody, a, c, n, v, roles):
                    return self._nobody.__of__(self)
                else:
                    # we can't authorize the user, and we either can't
                    # authorize nobody against the published object or
                    # we're not top-level
                    return None

    def _getobcontext(self, v, request):
        """
        v is the object (value) we're validating access to
        n is the name used to access the object
        a is the object the object was accessed through
        c is the physical container of the object
        """
        if len(request.steps) == 0: # someone deleted root index_html
            request.RESPONSE.notFoundError('no default view (root default view'
                                           ' was probably deleted)')
        n = request.steps[-1]
        # default to accessed and container as v.__parent__
        a = c = request['PARENTS'][0]
        # try to find actual container
        inner = getattr(v, 'aq_inner', v)
        innerparent = getattr(inner, '__parent__', None)
        if innerparent is not None:
            # this is not a method, we needn't treat it specially
            c = innerparent
        elif hasattr(v, 'im_self'):
            # this is a method, we need to treat it specially
            c = v.im_self
            c = getattr(v, 'aq_inner', v)
        request_container = getattr(request['PARENTS'][-1], '__parent__', [])
        # if pub's __parent__ or container is the request container, it
        # means pub was accessed from the root
        if a is request_container:
            a = request['PARENTS'][-1]
        if c is request_container:
            c = request['PARENTS'][-1]

        return a, c, n, v

    def _isTop(self):
        try:
            return aq_base(aq_parent(self)).isTopLevelPrincipiaApplicationObject
        except:
            return 0

    def __len__(self):
        return 1

    _mainUser=DTMLFile('dtml/mainUser', globals())
    _add_User=DTMLFile('dtml/addUser', globals(),
                       remote_user_mode__=_remote_user_mode)
    _editUser=DTMLFile('dtml/editUser', globals(),
                       remote_user_mode__=_remote_user_mode)
    manage=manage_main=_mainUser
    manage_main._setName('manage_main')

    _userFolderProperties = DTMLFile('dtml/userFolderProps', globals())

    def manage_userFolderProperties(self, REQUEST=None,
                                    manage_tabs_message=None):
        """
        """
        return self._userFolderProperties(
            self, REQUEST, manage_tabs_message=manage_tabs_message,
            management_view='Properties')

    @requestmethod('POST')
    def manage_setUserFolderProperties(self, encrypt_passwords=0,
                                       update_passwords=0,
                                       maxlistusers=DEFAULTMAXLISTUSERS,
                                       REQUEST=None):
        """
        Sets the properties of the user folder.
        """
        self.encrypt_passwords = not not encrypt_passwords
        try:
            self.maxlistusers = int(maxlistusers)
        except ValueError:
            self.maxlistusers = DEFAULTMAXLISTUSERS
        if encrypt_passwords and update_passwords:
            changed = 0
            for u in self.getUsers():
                pw = u._getPassword()
                if not self._isPasswordEncrypted(pw):
                    pw = self._encryptPassword(pw)
                    self._doChangeUser(u.getUserName(), pw, u.getRoles(),
                                       u.getDomains())
                    changed = changed + 1
            if REQUEST is not None:
                if not changed:
                    msg = 'All passwords already encrypted.'
                else:
                    msg = 'Encrypted %d password(s).' % changed
                return self.manage_userFolderProperties(
                    REQUEST, manage_tabs_message=msg)
            else:
                return changed
        else:
            if REQUEST is not None:
                return self.manage_userFolderProperties(
                    REQUEST, manage_tabs_message='Saved changes.')

    def _isPasswordEncrypted(self, pw):
        return AuthEncoding.is_encrypted(pw)

    def _encryptPassword(self, pw):
        return AuthEncoding.pw_encrypt(pw, 'SSHA')


    def domainSpecValidate(self,spec):

        for ob in spec:

            am = addr_match(ob)
            hm = host_match(ob)

            if am is None and hm is None:
                return 0

        return 1

    @requestmethod('POST')
    def _addUser(self,name,password,confirm,roles,domains,REQUEST=None):
        if not name:
            return MessageDialog(
                   title  ='Illegal value',
                   message='A username must be specified',
                   action ='manage_main')
        if not password or not confirm:
            if not domains:
                return MessageDialog(
                   title  ='Illegal value',
                   message='Password and confirmation must be specified',
                   action ='manage_main')
        if self.getUser(name) or (self._emergency_user and
                                  name == self._emergency_user.getUserName()):
            return MessageDialog(
                   title  ='Illegal value',
                   message='A user with the specified name already exists',
                   action ='manage_main')
        if (password or confirm) and (password != confirm):
            return MessageDialog(
                   title  ='Illegal value',
                   message='Password and confirmation do not match',
                   action ='manage_main')

        if not roles: roles=[]
        if not domains: domains=[]

        if domains and not self.domainSpecValidate(domains):
            return MessageDialog(
                   title  ='Illegal value',
                   message='Illegal domain specification',
                   action ='manage_main')
        self._doAddUser(name, password, roles, domains)
        if REQUEST: return self._mainUser(self, REQUEST)

    @requestmethod('POST')
    def _changeUser(self,name,password,confirm,roles,domains,REQUEST=None):
        if password == 'password' and confirm == 'pconfirm':
            # Protocol for editUser.dtml to indicate unchanged password
            password = confirm = None
        if not name:
            return MessageDialog(
                   title  ='Illegal value',
                   message='A username must be specified',
                   action ='manage_main')
        if password == confirm == '':
            if not domains:
                return MessageDialog(
                   title  ='Illegal value',
                   message='Password and confirmation must be specified',
                   action ='manage_main')
        if not self.getUser(name):
            return MessageDialog(
                   title  ='Illegal value',
                   message='Unknown user',
                   action ='manage_main')
        if (password or confirm) and (password != confirm):
            return MessageDialog(
                   title  ='Illegal value',
                   message='Password and confirmation do not match',
                   action ='manage_main')

        if not roles: roles=[]
        if not domains: domains=[]

        if domains and not self.domainSpecValidate(domains):
            return MessageDialog(
                   title  ='Illegal value',
                   message='Illegal domain specification',
                   action ='manage_main')
        self._doChangeUser(name, password, roles, domains)
        if REQUEST: return self._mainUser(self, REQUEST)

    @requestmethod('POST')
    def _delUsers(self,names,REQUEST=None):
        if not names:
            return MessageDialog(
                   title  ='Illegal value',
                   message='No users specified',
                   action ='manage_main')
        self._doDelUsers(names)
        if REQUEST: return self._mainUser(self, REQUEST)

    security.declareProtected(ManageUsers, 'manage_users')
    def manage_users(self,submit=None,REQUEST=None,RESPONSE=None):
        """This method handles operations on users for the web based forms
           of the ZMI. Application code (code that is outside of the forms
           that implement the UI of a user folder) are encouraged to use
           manage_std_addUser"""
        if submit=='Add...':
            return self._add_User(self, REQUEST)

        if submit=='Edit':
            try:    user=self.getUser(reqattr(REQUEST, 'name'))
            except: return MessageDialog(
                    title  ='Illegal value',
                    message='The specified user does not exist',
                    action ='manage_main')
            return self._editUser(self,REQUEST,user=user,password=user.__)

        if submit=='Add':
            name    =reqattr(REQUEST, 'name')
            password=reqattr(REQUEST, 'password')
            confirm =reqattr(REQUEST, 'confirm')
            roles   =reqattr(REQUEST, 'roles')
            domains =reqattr(REQUEST, 'domains')
            return self._addUser(name,password,confirm,roles,domains,REQUEST)

        if submit=='Change':
            name    =reqattr(REQUEST, 'name')
            password=reqattr(REQUEST, 'password')
            confirm =reqattr(REQUEST, 'confirm')
            roles   =reqattr(REQUEST, 'roles')
            domains =reqattr(REQUEST, 'domains')
            return self._changeUser(name,password,confirm,roles,
                                    domains,REQUEST)

        if submit=='Delete':
            names=reqattr(REQUEST, 'names')
            return self._delUsers(names,REQUEST)

        return self._mainUser(self, REQUEST)

    security.declareProtected(ManageUsers, 'user_names')
    def user_names(self):
        return self.getUserNames()

    def manage_beforeDelete(self, item, container):
        if item is self:
            try: del container.__allow_groups__
            except: pass

    def manage_afterAdd(self, item, container):
        if item is self:
            self = aq_base(self)
            container.__allow_groups__ = self

    def __creatable_by_emergency_user__(self): return 1

    def _setId(self, id):
        if id != self.id:
            raise MessageDialog(
                title='Invalid Id',
                message='Cannot change the id of a UserFolder',
                action ='./manage_main',)


    # Domain authentication support. This is a good candidate to
    # become deprecated in future Zope versions.

    security.declareProtected(ManageUsers, 'setDomainAuthenticationMode')
    def setDomainAuthenticationMode(self, domain_auth_mode):
        """Set the domain-based authentication mode. By default, this
           mode is off due to the high overhead of the operation that
           is incurred for all anonymous accesses. If you have the
           'Manage Users' permission, you can call this method via
           the web, passing a boolean value for domain_auth_mode to
           turn this behavior on or off."""
        v = self._domain_auth_mode = domain_auth_mode and 1 or 0
        return 'Domain authentication mode set to %d' % v

    def domainAuthModeEnabled(self):
        """ returns true if domain auth mode is set to true"""
        return getattr(self, '_domain_auth_mode', None)


class UserFolder(BasicUserFolder):

    """Standard UserFolder object

    A UserFolder holds User objects which contain information
    about users including name, password domain, and roles.
    UserFolders function chiefly to control access by authenticating
    users and binding them to a collection of roles."""

    implements(IStandardUserFolder)

    meta_type='User Folder'
    id       ='acl_users'
    title    ='User Folder'
    icon     ='p_/UserFolder'

    def __init__(self):
        self.data=PersistentMapping()

    def getUserNames(self):
        """Return a list of usernames"""
        names=self.data.keys()
        names.sort()
        return names

    def getUsers(self):
        """Return a list of user objects"""
        data=self.data
        names=data.keys()
        names.sort()
        return [data[n] for n in names]

    def getUser(self, name):
        """Return the named user object or None"""
        return self.data.get(name, None)

    def hasUsers(self):
        """ This is not a formal API method: it is used only to provide
        a way for the quickstart page to determine if the default user
        folder contains any users to provide instructions on how to
        add a user for newbies.  Using getUserNames or getUsers would have
        posed a denial of service risk."""
        return not not len(self.data)

    def _doAddUser(self, name, password, roles, domains, **kw):
        """Create a new user"""
        if password is not None and self.encrypt_passwords \
                                and not self._isPasswordEncrypted(password):
            password = self._encryptPassword(password)
        self.data[name]=User(name,password,roles,domains)

    def _doChangeUser(self, name, password, roles, domains, **kw):
        user=self.data[name]
        if password is not None:
            if (  self.encrypt_passwords
                  and not self._isPasswordEncrypted(password)):
                password = self._encryptPassword(password)
            user.__=password
        user.roles=roles
        user.domains=domains

    def _doDelUsers(self, names):
        for name in names:
            del self.data[name]

    def _createInitialUser(self):
        """
        If there are no users or only one user in this user folder,
        populates from the 'inituser' file in the instance home.
        We have to do this even when there is already a user
        just in case the initial user ignored the setup messages.
        We don't do it for more than one user to avoid
        abuse of this mechanism.
        Called only by OFS.Application.initialize().
        """
        if len(self.data) <= 1:
            info = readUserAccessFile('inituser')
            if info:
                import App.config
                name, password, domains, remote_user_mode = info
                self._doDelUsers(self.getUserNames())
                self._doAddUser(name, password, ('Manager',), domains)
                cfg = App.config.getConfiguration()
                try:
                    os.remove(os.path.join(cfg.instancehome, 'inituser'))
                except:
                    pass


InitializeClass(UserFolder)


def manage_addUserFolder(self,dtself=None,REQUEST=None,**ignored):
    """ """
    f=UserFolder()
    self=self.this()
    try:    self._setObject('acl_users', f)
    except: return MessageDialog(
                   title  ='Item Exists',
                   message='This object already contains a User Folder',
                   action ='%s/manage_main' % REQUEST['URL1'])
    self.__allow_groups__=f
    if REQUEST is not None:
        REQUEST['RESPONSE'].redirect(self.absolute_url()+'/manage_main')