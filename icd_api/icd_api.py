from datetime import datetime
import os
import time
from typing import Union, Optional
import urllib.parse

import requests
from requests_cache import CachedSession

from icd_api.linearization import Linearization
from icd_api.icd_util import get_foundation_uri
from icd_api.icd_entity import ICDEntity
from icd_api.icd_lookup import ICDLookup


class Api:
    def __init__(self,
                 base_url: str,
                 token_endpoint: Optional[str] = None,
                 client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 cached_session_config: Optional[dict] = None):
        self.base_url = base_url
        self.session = self.get_session(cached_session_config=cached_session_config)
        self.check_connection()

        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.linearization = None  # type: Union[Linearization, None]
        self.throttled = False

        if self.use_auth_token:
            self.cached_token_path = "../.token"
            self.token = self.get_token()
        else:
            self.cached_token_path = ""
            self.token = ""

    @staticmethod
    def get_session(cached_session_config: Optional[dict] = None) -> Union[requests.Session, CachedSession]:
        """
        Create a CachedSession if cached_session_config is provided, otherwise create a normal requests.Session

        :param cached_session_config: any kwargs that are accepted by CachedSession()
            Optionally include any kwargs that are accepted by CachedSession constructor.
            Typically, this includes "cache_name" and "backend".

            The minimum requirement is a value for key "cache_name" that is not None.
            If no "backend" is provided, the default is sqlite, and d["cache_name"] is a file path.
        :type cached_session_config: dict
        :return: a CachedSession if the required config was provided, otherwise a normal requests Session
        :rtype: Union[requests.Session, CachedSession]
        """
        if not cached_session_config or not cached_session_config.get("cache_name"):
            return requests.session()
        return CachedSession(**cached_session_config)

    def check_connection(self):
        """
        Check if the server is available - if it is not, raise an error with a helpful message
        """
        swagger_endpoint = f"{self.base_url.rstrip('/icd')}/swagger/index.html"
        try:
            self.session.get(swagger_endpoint)
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"Cannot connect to BASE_URL {self.base_url}") from None

    @property
    def use_cache(self) -> bool:
        """
        :return: whether the instance was created with a cache or not - see self.get_session()
        :rtype: bool
        """
        return isinstance(self.session, CachedSession)

    @property
    def token_is_valid(self) -> bool:
        """
        :return: whether a token exists and is younger than the allowed age --
                 tokens are valid for ~ 1 hr: https://icd.who.int/icdapi/docs2/API-Authentication/
        :rtype: bool
        """
        if os.path.exists(self.cached_token_path):
            date_created = os.path.getmtime(self.cached_token_path)
            token_age_seconds = datetime.now().timestamp() - date_created
            allowed_age_seconds = 60 * 60
            return token_age_seconds < allowed_age_seconds
        return False

    def get_token(self) -> str:
        """
        :return: authorization token, valid for up to one hour, may be cached in a local file self.cached_token_path
        :rtype: str
        """
        if self.token_endpoint is None:
            raise ValueError("No token endpoint provided")

        if self.token_is_valid:
            with open(self.cached_token_path, "r") as token_file:
                token = token_file.read()
                return token

        scope = 'icdapi_access'
        grant_type = 'client_credentials'
        payload = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': scope,
            'grant_type': grant_type,
        }

        r = requests.post(self.token_endpoint, data=payload, verify=False).json()
        token = r['access_token']

        with open(self.cached_token_path, "w") as token_file:
            token_file.write(token)

        return token

    @property
    def use_auth_token(self) -> bool:
        """
        If the target server is locally deployed, authentication is not implemented:
        https://icd.who.int/icdapi/docs2/ICDAPI-LocalDeployment/

        :return: whether the instance contains all required info for getting an OATH2 auth token
        :rtype: bool
        """
        return self.token_endpoint is not None and self.client_id is not None and self.client_secret is not None

    @property
    def headers(self) -> dict:
        """
        :return: HTTP header fields that are required for all requests (except for getting a token)
        :rtype: dict
        """
        # todo: language and version should not be hard-coded
        headers = {
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/json',
            'Accept-Language': 'en',
            'API-Version': 'v2',
        }
        return headers

    @property
    def current_release_id(self) -> str:
        if self.linearization:
            return self.linearization.current_release_id
        else:
            # todo: default release should not be hard-coded
            return "2023-01"

    def get_request(self, uri) -> Union[dict, None]:
        """
        helper method for making get requests (except for getting a token)

        :return: the response json object if 200
                 None if 404
                 all other status codes fail
        :rtype: Union[dict, None]
        """
        r = self.session.get(uri, headers=self.headers, verify=False)
        if r.status_code == 200:
            response_data = r.json()
            return response_data
        elif r.status_code == 404:
            return None
        else:
            raise ValueError(f"Api.get_request -- unexpected response {r.status_code}")

    def get_depth_recurse(self, entity_id: str) -> int:
        """
        keep getting parent until you get to the root, report back the depth

        todo: delete this - it's misleading as depth can vary based on which parent you choose
        """
        depth = 0
        entity = self.get_entity(entity_id=entity_id)
        while entity is not None:
            parent_id = entity.parent_ids[0]
            depth += 1
            entity = self.get_entity(parent_id)
        return depth - 1

    def get_residual_codes(self, entity_id, linearization_name: str = "mms") -> dict:
        """
        get Y-code and Z-code information for the provided entity, if they exist

        todo: linearization_name and current_release_id should both come from self.linearization,
              or pass in a Linearization object here
              same for a few other methods too (eg get_linearization_entity)
        """
        uris = {
            "Y": f"{self.base_url}/release/11/{self.current_release_id}/{linearization_name}/{entity_id}/other",
            "Z": f"{self.base_url}/release/11/{self.current_release_id}/{linearization_name}/{entity_id}/unspecified"
        }
        results = {"Y": None, "Z": None}
        for key, uri in uris.items():
            r = requests.get(uri, headers=self.headers, verify=False)
            if r.status_code == 200:
                results[key] = r.json()
            elif r.status_code == 404:
                results[key] = None
            else:
                raise ValueError(f"Api.get_residual_codes -- unexpected Response {r.status_code}")
        return results

    def get_entity(self, entity_id: str) -> Union[ICDEntity, None]:
        """
        get the response from ~/icd/entity/{entity_id}

        :param entity_id: id of an ICD-11 foundation entity
        :type entity_id: int
        :return: information on the specified ICD-11 foundation entity
        :rtype: ICDEntity
        """
        uri = f"{self.base_url}/entity/{entity_id}"
        if self.linearization and self.current_release_id:
            uri += f"?releaseId={self.current_release_id}"

        response_data = self.get_request(uri=uri)
        if response_data is None:
            return None

        return ICDEntity.from_api(entity_id=str(entity_id), response_data=response_data)

    def get_linearization_entity(self,
                                 entity_id: str,
                                 linearization_name: str,
                                 include: Optional[str] = None) -> Union[ICDLookup, None]:
        """
        get the response from ~/icd/release/11/{release_id}/{linearization_name}/{entity_id}

        :param entity_id: id of an ICD-11 foundation entity
        :type entity_id: int
        :param linearization_name: id of an ICD-11 linearization (eg mms)
        :type linearization_name: str
        :param include: optional attributes to include in the results ("ancestor" or "descendant")
        :type include: str
        :return: linearization-specific information on the specified ICD-11 entity
        :rtype: ICDLookup
        """
        uri = f"{self.base_url}/release/11/{self.current_release_id}/{linearization_name}/{entity_id}"
        if include:
            if include.lower() not in ["ancestor", "descendant"]:
                raise ValueError(f"Unexpected include value '{include}' (expected 'ancestor' or 'descendant')")
            uri += f"?include={include.lower()}"

        response_data = self.get_request(uri=uri)
        if response_data is None:
            return None

        foundation_uri = get_foundation_uri(entity_id=entity_id)
        return ICDLookup.from_api(request_uri=foundation_uri, response_data=response_data)

    def get_linearization_descendent_ids(self, entity_id: str, linearization_name: str) -> Union[list, None]:
        """
        get all descendents of the provided entity, in the context of the provided linearization

        :param entity_id: id of an ICD-11 foundation entity
        :type entity_id: int
        :param linearization_name: id of an ICD-11 linearization (eg mms)
        :type linearization_name: str
        :return: list of descendant entity_ids
        :rtype: list
        """
        obj = self.get_linearization_entity(entity_id=entity_id,
                                            linearization_name=linearization_name,
                                            include="descendant")
        if obj:
            return obj.descendant_ids
        return None

    def get_linearization_ancestor_ids(self, entity_id: str, linearization_name: str) -> Union[list, None]:
        """
        get all ancestors of the provided entity, in the context of the provided linearization

        :param entity_id: id of an ICD-11 foundation entity
        :type entity_id: int
        :param linearization_name: id of an ICD-11 linearization (eg mms)
        :type linearization_name: str
        :return: list of descendant entity_ids
        :rtype: list
        """
        obj = self.get_linearization_entity(entity_id=entity_id,
                                            linearization_name=linearization_name,
                                            include="ancestor")
        if obj:
            return obj.ancestor_ids
        return None

    def get_entity_full(self, entity_id: str) -> ICDEntity:
        """
        :param entity_id: id of an ICD-11 foundation entity
        :type entity_id: int
        :return: results of /entity and /lookup and /residual
        :rtype: dict
        """
        icd_entity = self.get_entity(entity_id=entity_id)
        if icd_entity is None:
            raise ValueError(f"entity_id {entity_id} not found")

        icd_entity.residuals = self.get_residual_codes(entity_id=entity_id)
        foundation_uri = get_foundation_uri(entity_id)
        lookup = self.lookup(foundation_uri=foundation_uri)
        icd_entity.lookup = lookup
        return icd_entity

    def get_ancestors(self,
                      entity_id: str,
                      entities: Optional[list],
                      depth: int = 0,
                      nested_output: bool = True) -> list:
        """
        get all entities listed under entity.child, recursively

        :param entity_id: entity_id to look up - initially the root
        :param entities: list of already-traversed entities (initially empty)
        :param depth: current depth
        :param nested_output: whether to store child nodes in a nested structure, False = flattened
        :return: full list of all ancestry under the root
        :rtype: list
        """
        if entities is None:
            entities = []

        icd_entity = self.get_entity(entity_id=entity_id)
        if icd_entity is None:
            raise ValueError(f"entity_id {entity_id} not found")

        icd_entity.depth = depth

        print(f"{' '*depth} get_entity: {icd_entity}")

        if nested_output:
            icd_entity.child_entities = []  # type: ignore

        entities.append(icd_entity)

        for child_id in icd_entity.child_ids:
            existing = next(iter([e for e in entities if e.entity_id == child_id]), None)
            if existing is None:
                if nested_output:
                    self.get_ancestors(entities=icd_entity.child_entities,  # type: ignore
                                       entity_id=child_id,
                                       depth=depth + 1,
                                       nested_output=nested_output)
                else:
                    self.get_ancestors(entities=entities,
                                       entity_id=child_id,
                                       depth=depth + 1,
                                       nested_output=nested_output)
        return entities

    def get_leaf_nodes(self, entity_id: str, entities: list) -> list:
        """
        get leaf entities, those with no children of their own

        :param entity_id: entity_id to look up - initially the root
        :param entities: list of already-traversed entities (initially empty)
        :return: list of all leaf node ids
        :rtype: list[str]
        """
        entity = self.get_entity(entity_id=entity_id)
        if entity is None:
            raise ValueError(f"entity_id {entity_id} not found")

        if not entity.child_ids:
            # this is a leaf node
            entities.append(entity_id)
        else:
            for child_id in entity.child_ids:
                existing = next(iter([e for e in entities if e == child_id]), None)
                if existing is None:
                    self.get_leaf_nodes(entities=entities, entity_id=child_id)
        return entities

    def search(self, uri) -> dict:
        """
        get the response from a post request to ~/entity/search?q={search_string}

        todo: rename this to post_request - this appears to have been conflated with search_entities
        """
        r = requests.post(uri, headers=self.headers, verify=False)
        results = r.json()
        if results["error"]:
            raise ValueError(results["errorMessage"])
        return results

    def search_entities(self, search_string: str) -> list:
        """
        search all foundation entities for the provided search string

        :param search_string: value to search for
        :type search_string: str
        :return: search results as a list of objects
        :rtype: list
        """
        uri = f"{self.base_url}/entity/search?q={search_string}"
        results = self.search(uri=uri)
        return results["destinationEntities"]

    def set_linearization(self, linearization_name: str, release_id: Optional[str]) -> Linearization:
        """
        :return: basic information on the linearization together with the list of available releases
        :rtype: linearization
        """
        uri = f"{self.base_url}/release/11/{linearization_name}"
        all_releases = self.get_request(uri=uri)
        if all_releases is None:
            raise ValueError(f"linearization {linearization_name} not found")

        # Note: the endpoint responds with http urls of all releases which feed into other properties -
        #       this local `linearization_base_url` definition safeguards against self.base_url values that are https
        linearization_base_url = self.base_url.replace("https://", "http://")
        linearization = Linearization(
            context=all_releases["@context"],
            oid=all_releases["@id"],
            title=all_releases["title"],
            latest_release_uri=all_releases["latestRelease"],
            current_release_uri=all_releases["latestRelease"],
            releases=all_releases["release"],
            base_url=linearization_base_url,
        )

        if release_id:
            # make sure the provided release_id is valid
            release_ids = linearization.release_ids
            if release_id not in release_ids:
                raise ValueError(f"release_id {release_id} not in available releases {','.join(release_ids)}")
            linearization.current_release_uri = f"{linearization.base_url}/release/11/{release_id}/{linearization_name}"

        self.linearization = linearization
        return linearization

    def get_entity_linearization_releases(self, entity_id: int, linearization_name: str = "mms") -> list:
        """
        get the response from ~/icd/release/11/{linearization_name}/{entity_id}

        :return: a list of URIs to the entity in the releases for which the entity is available
        :rtype: List
        """
        # todo: why is this not using self.get_request()?
        uri = f"{self.base_url}/release/11/{linearization_name}/{entity_id}"
        r = requests.get(uri, headers=self.headers, verify=False)

        results = r.json()
        return results

    def get_uri(self, uri: str) -> list:
        """
        :return: a list of URIs of the entity in the available releases
        :rtype: List
        """
        url = f"{self.base_url}/{uri}"
        r = requests.get(url, headers=self.headers, verify=False)

        results = r.json()
        return results

    def get_url(self, url: str) -> list:
        """
        :return: a list of URIs of the entity in the available releases
        :rtype: List
        """
        r = requests.get(url, headers=self.headers, verify=False)

        results = r.json()
        return results

    def get_icd10_codes(self, url: str, items: list, depth: int = 0) -> list:
        """
        get all icd10 codes recursively, throttled to not overload the servers
        note: a local deployment of the WHO API does not contain ICD 10 endpoints,
        so this needs to be run agains the public one

        :return: a list of URIs of the entity in the available releases
        :rtype: List
        """
        max_depth = 0
        r = requests.get(url, headers=self.headers, verify=False)
        time.sleep(0.5)

        if r.status_code == 200:
            self.throttled = False
            results = r.json()
            items.append(results)
            if depth <= max_depth:
                for child in results.get("child", []):
                    self.get_icd10_codes(url=child, items=items, depth=depth + 1)
            return items
        elif r.status_code == 401:
            if self.throttled and self.token_is_valid:
                # 401 Unauthorized, even after throttling and requesting a new token
                raise ConnectionRefusedError("got 401 even after throttling and requesting a new token")

            print("401 - waiting 10 minutes")
            self.throttled = True
            time.sleep(600)

            if not self.token_is_valid:
                print("401 - requesting new token")
                self.token = self.get_token()

            return self.get_icd10_codes(url=url, items=items, depth=depth)
        else:
            raise ConnectionError(f"error {r.status_code}", r)

    def get_code(self, icd_version: int, code: str) -> Union[dict, None]:
        """
        :param icd_version: code version (10 or 11)
        :type icd_version: int
        :param code: code to lookup
        :type code: str
        :return: a list of URIs of the entity in the available releases
        :rtype: List
        """
        if icd_version == 10:
            uri = f"{self.base_url}/release/10/{code}"
        else:
            quoted_code = urllib.parse.quote(code, safe="")
            uri = f"{self.base_url}/release/11/{self.current_release_id}/mms/codeinfo/{quoted_code}?flexiblemode=true"
        response_data = self.get_request(uri=uri)
        return response_data

    def lookup(self, foundation_uri: str) -> Union[ICDLookup, None]:
        """
        This endpoint allows looking up a foundation entity within the mms linearization
        and returns where that entity is coded in this linearization.

        If the foundation entity is included in the linearization and has a code then that linearization entity
        is returned. If the foundation entity in included in the linearization but it is a grouping without a code
        then the system will return the unspecified residual category under that grouping.

        If the entity is not included in the linearization then the system checks where that entity
        is aggregated to and then returns that entity.
        """
        quoted_url = urllib.parse.quote(foundation_uri, safe='')
        uri = f"{self.base_url}/release/11/{self.current_release_id}/mms/lookup?foundationUri={quoted_url}"

        response_data = self.get_request(uri=uri)
        if response_data is None:
            return None

        entity = ICDLookup.from_api(request_uri=foundation_uri, response_data=response_data)
        return entity

    def search_linearization(self, search_string: str) -> dict:
        """
        get the response from ~/icd/release/11/{release_id}/{linearization_name}/{search_string}
        """
        uri = f"{self.base_url}/release/11/{self.current_release_id}/mms/search?q={search_string}"
        results = self.search(uri=uri)
        return results["destinationEntities"]

    @classmethod
    def from_environment(cls):
        base_url = os.environ["BASE_URL"]
        token_endpoint = os.environ["TOKEN_ENDPOINT"]
        client_id = os.environ["CLIENT_ID"]
        client_secret = os.environ["CLIENT_SECRET"]

        cached_session_config = {
            "cache_name": os.getenv("REQUESTS_CACHE_NAME"),
            "backend": os.getenv("REQUESTS_CACHE_BACKEND", "sqlite")
        }

        return cls(base_url=base_url,
                   token_endpoint=token_endpoint,
                   client_id=client_id,
                   client_secret=client_secret,
                   cached_session_config=cached_session_config)


if __name__ == "__main__":
    api = Api.from_environment()

    root_icd11_entity = api.get_entity("455013390")
    root_icd10_entity = api.get_uri("release/10/2019")
    search_results = api.search_entities(search_string="diabetes")
    print(search_results)
