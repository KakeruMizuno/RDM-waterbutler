import logging
import aiohttp

from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions
from waterbutler.core.path import WaterButlerPath

from waterbutler.providers.nextcloud import utils
from waterbutler.providers.nextcloud.metadata import NextcloudFileRevisionMetadata

logger = logging.getLogger(__name__)


class NextcloudProvider(provider.BaseProvider):
    """Provider for the Nextcloud cloud storage service.

    This provider uses WebDAV for communication.

    API docs::

    * WebDAV: http://www.webdav.org/specs/rfc4918.html
    * OCSv1.7: https://www.freedesktop.org/wiki/Specifications/open-collaboration-services-1.7/

    Required settings fields::

    * folder
    * verify_ssl

    Required credentials fields::

    * host
    * username
    * password

    Quirks:

    * User credentials are stored in a aiohttp.BasicAuth object. At the moment, there isn't a
      better way to do this.
    """
    NAME = 'nextcloud'

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)

        self.folder = settings['folder']
        if not self.folder.endswith('/'):
            self.folder += '/'

        self.verify_ssl = settings['verify_ssl']
        self.url = credentials['host']
        self._auth = aiohttp.BasicAuth(credentials['username'], credentials['password'])
        self.metrics.add('host', self.url)

    def connector(self):
        return aiohttp.TCPConnector(verify_ssl=self.verify_ssl)

    @property
    def _webdav_url_(self):
        """Formats the outgoing url appropriately. This accounts for some differences in oc server
        software.
        """
        if self.url[-1] != '/':
            return self.url + '/remote.php/webdav/'
        return self.url + 'remote.php/webdav/'

    @property
    def _dav_url_(self):
        """ Formats the outgoing url appropriately. This is used like below.
        https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/versions.html
        """
        if self.url[-1] != '/':
            return self.url + '/remote.php/dav/'
        return self.url + 'remote.php/dav/'

    @property
    def _ocs_url(self):
        """ Formats the outgoing url appropriately. This is used like below.
        https://docs.nextcloud.com/server/latest/developer_manual/client_apis/OCS/ocs-api-overview.html
        """
        if self.url[-1] != '/':
            return self.url + '/ocs/v2.php/'
        return self.url + 'ocs/v2.php/'

    def shares_storage_root(self, other):
        """Nextcloud settings only include the root folder. If a cross-resource move occurs
        between two nextcloud providers that are on different accounts but have the same folder
        base name, the parent method could incorrectly think the action is a self-overwrite.
        Comparing credentials means that this is unique per connected account.

        :param waterbutler.core.provider.BaseProvider other: another provider to test
        :return: `True` if both providers share the same storage root
        :rtype: `bool`
        """
        return super().shares_storage_root(other) and self.credentials == other.credentials

    async def validate_v1_path(self, path, **kwargs):
        """Verifies that ``path`` exists and if so, returns a WaterButlerPath object that
        represents it. WebDAV returns 200 for a single file, 207 for a multipart (folder), and 404
        for Does Not Exist.

        :param str path: user-supplied path to validate
        :return: WaterButlerPath object representing ``path``
        :rtype: `waterbutler.core.path.WaterButlerPath`
        :raises `waterbutler.core.exceptions.NotFoundError`: if the path doesn't exist
        """
        if path == '/':
            return WaterButlerPath(path, prepend=self.folder)
        full_path = WaterButlerPath(path, prepend=self.folder)

        response = await self.make_request('PROPFIND',
            self._webdav_url_ + full_path.full_path,
            expects=(200, 207, 404),
            throws=exceptions.MetadataError,
            auth=self._auth,
            connector=self.connector(),
        )
        content = await response.content.read()
        await response.release()
        if response.status == 404:
            raise exceptions.NotFoundError(str(full_path.full_path))

        try:
            item = await utils.parse_dav_response(self.NAME, content, '/')
        except exceptions.NotFoundError:
            # Re-raise with the proper path
            raise exceptions.NotFoundError(str(full_path.full_path))
        if full_path.kind != item[0].kind:
            raise exceptions.NotFoundError(full_path.full_path)
        return full_path

    async def validate_path(self, path, **kwargs):
        """Similar to `validate_v1_path`, but will not throw a 404 if the path doesn't yet exist.
        Instead, returns a WaterButlerPath object for the potential path (such as before uploads).

        :param str path: user-supplied path to validate
        :return: WaterButlerPath object representing ``path``
        :rtype: :class:`waterbutler.core.path.WaterButlerPath`
        """
        if path == '/':
            return WaterButlerPath(path, prepend=self.folder)
        full_path = WaterButlerPath(path, prepend=self.folder)
        response = await self.make_request('PROPFIND',
            self._webdav_url_ + full_path.full_path,
            expects=(200, 207, 404),
            throws=exceptions.MetadataError,
            auth=self._auth,
            connector=self.connector(),
        )
        content = await response.content.read()
        await response.release()

        try:
            await utils.parse_dav_response(self.NAME, content, '/')
        except exceptions.NotFoundError:
            pass
        return full_path

    async def download(self, path, accept_url=False, range=None, **kwargs):
        """Creates a stream for downloading files from the remote host. If the metadata query for
        the file has no size metadata, downloads to memory.

        :param waterbutler.core.path.WaterButlerPath path: user-supplied path to download
        :raises: `waterbutler.core.exceptions.DownloadError`
        """

        revision = None
        if 'revision' in kwargs:
            if kwargs['revision'] is not None:
                revision = kwargs['revision']

        self.metrics.add('download', {
            'got_accept_url': accept_url is False,
            'got_range': range is not None,
        })

        if revision is None:
            download_resp = await self.make_request(
                'GET',
                self._webdav_url_ + path.full_path,
                range=range,
                expects=(200, 206,),
                throws=exceptions.DownloadError,
                auth=self._auth,
                connector=self.connector(),
            )
        else:
            revisions = await self._metadata_revision(path)
            fileid = revisions[0].fileid
            etag = revision
            download_resp = await self.make_request(
                'GET',
                self._dav_url_ + 'versions/' + self.credentials['username'] + '/versions/' + fileid + '/' + etag,
                range=range,
                expects=(200, 206,),
                throws=exceptions.DownloadError,
                auth=self._auth,
                connector=self.connector(),
            )

        return streams.ResponseStreamReader(download_resp)

    async def upload(self, stream, path, conflict='replace', **kwargs):
        """Utilizes default name conflict handling behavior then adds the appropriate headers and
        creates the upload request.

        :param waterbutler.core.streams.RequestStreamReader stream: stream containing file contents
        :param waterbutler.core.path.WaterButlerPath path: user-supplied path to upload to
        :raises: `waterbutler.core.exceptions.UploadError`
        """
        if path.identifier and conflict == 'keep':
            path, _ = await self.handle_name_conflict(path, conflict=conflict, kind='folder')
            path._parts[-1]._id = None

        response = await self.make_request(
            'PUT',
            self._webdav_url_ + path.full_path,
            data=stream,
            headers={'Content-Length': str(stream.size)},
            expects=(201, 204,),
            throws=exceptions.UploadError,
            auth=self._auth,
            connector=self.connector(),
        )
        await response.release()
        meta = await self.metadata(path)
        return meta, response.status == 201

    async def delete(self, path, **kwargs):
        """Deletes ``path`` on remote host

        :param waterbutler.core.path.WaterButlerPath path: user-supplied path to delete
        :raises: `waterbutler.core.exceptions.DeleteError`
        """
        delete_resp = await self.make_request(
            'DELETE',
            self._webdav_url_ + path.full_path,
            expects=(204,),
            throws=exceptions.DeleteError,
            auth=self._auth,
            connector=self.connector(),
        )
        await delete_resp.release()
        return

    async def metadata(self, path, **kwargs):
        """Queries the remote host for metadata and returns metadata objects based on the return
        value.

        :param waterbutler.core.path.WaterButlerPath path: user-supplied path to query
        :raises: `waterbutler.core.exceptions.MetadataError`
        """
        if path.is_dir:
            return (await self._metadata_folder(path, **kwargs))
        else:
            return (await self._metadata_file(path, **kwargs))

    async def _metadata_file(self, path, **kwargs):
        items = await self._metadata_folder(path, skip_first=False, **kwargs)
        return items[0]

    async def _metadata_folder(self, path, skip_first=True, **kwargs):
        """Performs the actual query against Nextcloud. In this case the return code depends on the
        content::

            * 204: Empty response
            * 207: Multipart response
        """
        response = await self.make_request('PROPFIND',
            self._webdav_url_ + path.full_path,
            expects=(204, 207),
            throws=exceptions.MetadataError,
            auth=self._auth,
            connector=self.connector(),
        )

        items = []
        if response.status == 207:
            content = await response.content.read()
            items = await utils.parse_dav_response(self.NAME, content, self.folder, skip_first)
        await response.release()

        for i in items:
            if i.is_file and self.NAME == 'nextcloudinstitutions':
                params = {
                    'path': i._href,
                    'hash': 'md5,sha256,sha512'
                }
                response = await self.make_request('GET',
                    self._ocs_url + 'apps/checksum_api/api/checksum',
                    params=params,
                    expects=(200, 404),
                    throws=exceptions.MetadataError,
                    auth=self._auth,
                    connector=self.connector(),
                    headers={'OCS-APIRequest': 'true'}
                )

                if response.status == 200:
                    content = await response.content.read()
                    extra = {}
                    extra['hashes'] = await utils.parse_checksum_response(content)
                    i.extra = extra
                await response.release()

        return items

    async def _metadata_revision(self, path):
        query = '<?xml version="1.0" encoding="UTF-8"?> <d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns" > <d:prop xmlns:oc="http://owncloud.org/ns"> <d:getlastmodified/> <d:getcontentlength/> <d:resourcetype/> <d:getetag/> <d:getcontenttype/> <oc:fileid/>  </d:prop> </d:propfind>'

        response = await self.make_request('PROPFIND',
            self._webdav_url_ + path.full_path,
            data=query,
            expects=(204, 207),
            throws=exceptions.MetadataError,
            auth=self._auth,
            connector=self.connector(),
        )

        items = []
        if response.status == 207:
            content = await response.content.read()
            items = await utils.parse_dav_response(self.NAME, content, self.folder, False)
        await response.release()

        if len(items) != 1:
            return items

        if self.NAME == 'nextcloudinstitutions':
            params = {
                'path': path.full_path,
                'hash': 'md5,sha256,sha512'
            }
            response = await self.make_request('GET',
                self._ocs_url + 'apps/checksum_api/api/checksum',
                params=params,
                expects=(200, 404),
                throws=exceptions.MetadataError,
                auth=self._auth,
                connector=self.connector(),
                headers={'OCS-APIRequest': 'true'}
            )

            if response.status == 200:
                content = await response.content.read()
                extra = {}
                extra['hashes'] = await utils.parse_checksum_response(content)
                items[0].extra = extra
            await response.release()

        fileid = items[0].fileid

        response = await self.make_request('PROPFIND',
            self._dav_url_ + 'versions/' + self.credentials['username'] + '/versions/' + fileid,
            expects=(204, 207, 404),
            throws=exceptions.MetadataError,
            auth=self._auth,
            connector=self.connector(),
        )

        revision_items = []
        if response.status == 207:
            content = await response.content.read()
            revision_items = await utils.parse_dav_response(self.NAME, content, self.folder, True)
        await response.release()

        if self.NAME == 'nextcloudinstitutions':
            for rev in revision_items:
                params = {
                    'path': path.full_path,
                    'hash': 'md5,sha256,sha512',
                    'revision': str(rev.etag)
                }
                response = await self.make_request('GET',
                    self._ocs_url + 'apps/checksum_api/api/checksum',
                    params=params,
                    expects=(200, 404),
                    throws=exceptions.MetadataError,
                    auth=self._auth,
                    connector=self.connector(),
                    headers={'OCS-APIRequest': 'true'}
                )

                if response.status == 200:
                    content = await response.content.read()
                    extra = {}
                    extra['hashes'] = await utils.parse_checksum_response(content)
                    rev.extra = extra
                await response.release()

        items.extend(revision_items)

        return items

    async def create_folder(self, path, **kwargs):
        """Create a folder in the current provider at ``path``. Returns an
        `.metadata.NextcloudFolderMetadata` object if successful.

        :param waterbutler.core.path.WaterButlerPath path: user-supplied directory path to create
        :param boolean precheck_folder: flag to check for folder before attempting create
        :rtype: `.metadata.NextcloudFolderMetadata`
        :raises: `waterbutler.core.exceptions.CreateFolderError`
        """
        resp = await self.make_request(
            'MKCOL',
            self._webdav_url_ + path.full_path,
            expects=(201, 405),
            throws=exceptions.CreateFolderError,
            auth=self._auth,
            connector=self.connector()
        )
        await resp.release()
        if resp.status == 405:
            raise exceptions.FolderNamingConflict(path.name)
        # get the folder metadata
        meta = await self.metadata(path.parent)
        return [m for m in meta if m.path == path.materialized_path][0]

    def can_duplicate_names(self):
        return True

    def can_intra_copy(self, dest_provider, path=None):
        return self == dest_provider

    def can_intra_move(self, dest_provider, path=None):
        return self == dest_provider

    async def intra_copy(self, dest_provider, src_path, dest_path):
        return await self._do_dav_move_copy(src_path, dest_path, 'COPY')

    async def intra_move(self, dest_provider, src_path, dest_path):
        return await self._do_dav_move_copy(src_path, dest_path, 'MOVE')

    async def _do_dav_move_copy(self, src_path, dest_path, operation):
        """Performs a quick copy or move operation on the remote host.

        :param waterbutler.core.path.WaterButlerPath src_path: path for the source object
        :param waterbutler.core.path.WaterButlerPath dest_path: path for the destination object
        :param str operation: Either `COPY` or `MOVE`
        :rtype: `.metadata.NextcloudFileMetadata`
        :rtype: `.metadata.NextcloudFolderMetadata`
        :raises: `waterbutler.core.exceptions.IntraCopyError`
        """
        if operation != 'MOVE' and operation != 'COPY':
            raise NotImplementedError("Nextcloud move/copy only supports MOVE and COPY endpoints")

        resp = await self.make_request(
            operation,
            self._webdav_url_ + src_path.full_path,
            expects=(201, 204),  # WebDAV MOVE/COPY: 201 = Created, 204 = Updated existing
            throws=exceptions.IntraCopyError,
            auth=self._auth,
            connector=self.connector(),
            headers={'Destination': '/remote.php/webdav' + dest_path.full_path}
        )
        await resp.release()

        file_meta = await self.metadata(dest_path)
        if dest_path.is_folder:
            parent_meta = await self.metadata(dest_path.parent)
            meta = [m for m in parent_meta if m.materialized_path == dest_path.materialized_path][0]
            meta.children = file_meta
        else:
            meta = file_meta

        return meta, resp.status == 201

    async def revisions(self, path, **kwargs):
        revisions = await self._metadata_revision(path)
        items = []
        latest = len(revisions)
        for i in range(latest):
            r = revisions[i]
            ver = str(r.etag_noquote)
            items.append(NextcloudFileRevisionMetadata.from_metadata(self.NAME, ver, r))
        return items
