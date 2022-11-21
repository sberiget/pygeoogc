"""Some utilities for PyGeoOGC."""
from __future__ import annotations

import itertools
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Tuple, TypeVar, Union

import async_retriever as ar
import defusedxml.ElementTree as ETree
import pyproj
import requests
import shapely.geometry as sgeom
import ujson as json
import urllib3
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from requests_cache import CachedSession, Response
from requests_cache.backends.sqlite import SQLiteCache
from shapely import ops

from .exceptions import InputTypeError, ServiceError

CRSTYPE = Union[int, str, pyproj.CRS]
BOX_ORD = "(west, south, east, north)"
G = TypeVar(
    "G",
    sgeom.Point,
    sgeom.MultiPoint,
    sgeom.Polygon,
    sgeom.MultiPolygon,
    sgeom.LineString,
    sgeom.MultiLineString,
    Tuple[float, float, float, float],
    List[Tuple[float, float]],
)


def check_response(resp: str) -> str:
    """Extract error message from a response, if any."""
    try:
        root = ETree.fromstring(resp)
    except ETree.ParseError:
        return resp
    else:
        try:
            return str(root[-1][0].text).strip()
        except IndexError:
            return str(root[-1].text).strip()


class RetrySession:
    """Configures the passed-in session to retry on failed requests.

    Notes
    -----
    The fails can be due to connection errors, specific HTTP response
    codes and 30X redirections. The code was originally based on:
    https://github.com/bustawin/retry-requests

    Parameters
    ----------
    retries : int, optional
        The number of maximum retries before raising an exception, defaults to 5.
    backoff_factor : float, optional
        A factor used to compute the waiting time between retries, defaults to 0.5.
    status_to_retry : tuple, optional
        A tuple of status codes that trigger the reply behaviour, defaults to (500, 502, 504).
    prefixes : tuple, optional
        The prefixes to consider, defaults to ("http://", "https://")
    cache_name : str, optional
        Path to a folder for caching the session, default to None which uses
        system's temp directory.
    expire_after : int, optional
        Expiration time for the cache in seconds, defaults to -1 (never expire).
    disable : bool, optional
        If ``True`` temporarily disable caching requests and get new responses
        from the server, defaults to ``False``.
    ssl : bool, optional
        If ``True`` verify SSL certificates, defaults to ``True``.
    """

    def __init__(
        self,
        retries: int = 3,
        backoff_factor: float = 0.3,
        status_to_retry: tuple[int, ...] = (500, 502, 504),
        prefixes: tuple[str, ...] = ("https://",),
        cache_name: str | Path | None = None,
        expire_after: int = -1,
        disable: bool = False,
        ssl: bool = True,
    ) -> None:
        disable = os.getenv("HYRIVER_CACHE_DISABLE", f"{disable}").lower() == "true"
        if disable:
            self.session = requests.Session()
        else:
            self.cache_name = (
                Path("cache", "http_cache.sqlite") if cache_name is None else Path(cache_name)
            )
            backend = SQLiteCache(self.cache_name, fast_save=True, timeout=1)
            self.session = CachedSession(
                expire_after=int(os.getenv("HYRIVER_CACHE_EXPIRE", expire_after)), backend=backend
            )

        if not ssl:
            urllib3.disable_warnings()
            self.session.verify = False

        adapter = HTTPAdapter(
            max_retries=urllib3.Retry(
                total=retries,
                read=retries,
                connect=retries,
                backoff_factor=backoff_factor,
                status_forcelist=status_to_retry,
                allowed_methods=False,
            )
        )
        for prefix in prefixes:
            self.session.mount(prefix, adapter)

    def get(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        stream: bool | None = None,
    ) -> Response:
        """Retrieve data from a url by GET and return the Response."""
        resp = self.session.get(url, params=payload, headers=headers, stream=stream)
        try:
            resp.raise_for_status()
        except RequestException as ex:
            raise ServiceError(check_response(resp.text)) from ex
        else:
            return resp

    def post(
        self,
        url: str,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        stream: bool | None = None,
    ) -> Response:
        """Retrieve data from a url by POST and return the Response."""
        resp = self.session.post(url, data=payload, headers=headers, stream=stream)
        try:
            resp.raise_for_status()
        except RequestException as ex:
            raise ServiceError(check_response(resp.text)) from ex
        else:
            return resp

    def head(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> Response:
        """Retrieve data from a url by POST and return the Response."""
        resp = self.session.head(url, data=data, params=params, headers=headers)
        try:
            resp.raise_for_status()
        except RequestException as ex:
            raise ServiceError(check_response(resp.text)) from ex
        else:
            return resp

    def close(self) -> None:
        """Close the session."""
        self.session.close()


def traverse_json(
    items: dict[str, Any] | list[dict[str, Any]], ipath: str | list[str]
) -> list[Any]:
    """Extract an element from a JSON file along a specified ipath.

    This function is based on `bcmullins <https://bcmullins.github.io/parsing-json-python/>`__.

    Parameters
    ----------
    items : dict
        The input json dictionary
    ipath : list
        The ipath to the requested element

    Returns
    -------
    list
        The sub_items founds in the JSON

    Examples
    --------
    >>> from pygeoogc.utils import traverse_json
    >>> data = [{
    ...     "employees": [
    ...         {"name": "Alice", "role": "dev", "nbr": 1},
    ...         {"name": "Bob", "role": "dev", "nbr": 2}],
    ...     "firm": {"name": "Charlie's Waffle Emporium", "location": "CA"},
    ... },]
    >>> traverse_json(data, ["employees", "name"])
    [['Alice', 'Bob']]
    """

    def extract(
        sub_items: list[Any] | dict[str, Any] | None,
        path: str | list[str],
        ind: int,
        arr: list[Any],
    ) -> list[Any]:
        key = path[ind]
        if ind + 1 < len(path):
            if isinstance(sub_items, dict):
                if key in sub_items:
                    extract(sub_items.get(key), path, ind + 1, arr)
                else:
                    arr.append(None)
            elif isinstance(sub_items, list):
                if not sub_items:
                    arr.append(None)
                else:
                    for i in sub_items:
                        extract(i, path, ind, arr)
            else:
                arr.append(None)
        if ind + 1 == len(path):
            if isinstance(sub_items, list):
                if not sub_items:
                    arr.append(None)
                else:
                    for i in sub_items:
                        arr.append(i.get(key))
            elif isinstance(sub_items, dict):
                arr.append(sub_items.get(key))
            else:
                arr.append(None)
        return arr

    if isinstance(items, dict):
        return extract(items, ipath, 0, [])

    outer_arr = []
    for item in items:
        outer_arr.append(extract(item, ipath, 0, []))
    return outer_arr


@dataclass
class ESRIGeomQuery:
    """Generate input geometry query for ArcGIS RESTful services.

    Parameters
    ----------
    geometry : tuple or sgeom.Polygon or sgeom.Point or sgeom.LineString
        The input geometry which can be a point (x, y), a list of points [(x, y), ...],
        bbox (xmin, ymin, xmax, ymax), or a Shapely's sgeom.Polygon.
    wkid : int
        The Well-known ID (WKID) of the geometry's spatial reference e.g., for EPSG:4326,
        4326 should be passed. Check
        `ArcGIS <https://developers.arcgis.com/rest/services-reference/geographic-coordinate-systems.htm>`__
        for reference.
    """

    geometry: (
        tuple[float, float]
        | list[tuple[float, float]]
        | tuple[float, float, float, float]
        | sgeom.Polygon
        | sgeom.LineString
    )
    wkid: int

    def _get_payload(self, geo_type: str, geo_json: dict[str, Any]) -> Mapping[str, str]:
        """Generate a request payload based on ESRI template.

        Parameters
        ----------
        geo_type : str
            Type of the input geometry
        geo_json : dict
            Geometry in GeoJson format.

        Returns
        -------
        dict
            An ESRI geometry payload.
        """
        esri_json = json.dumps({**geo_json, "spatialRelference": {"wkid": str(self.wkid)}})
        return {
            "geometryType": geo_type,
            "geometry": esri_json,
            "inSR": str(self.wkid),
        }

    def point(self) -> Mapping[str, str]:
        """Query for a point."""
        if not (isinstance(self.geometry, tuple) and len(self.geometry) == 2):
            raise InputTypeError("geometry", "tuple", "(x, y)")

        geo_type = "esriGeometryPoint"
        geo_json = dict(zip(("x", "y"), self.geometry))
        return self._get_payload(geo_type, geo_json)

    def multipoint(self) -> Mapping[str, str]:
        """Query for a multi-point."""
        if not (isinstance(self.geometry, list) and all(len(g) == 2 for g in self.geometry)):
            raise InputTypeError("geometry", "list of tuples", "[(x, y), ...]")

        geo_type = "esriGeometryMultipoint"
        geo_json = {"points": [[x, y] for x, y in self.geometry]}
        return self._get_payload(geo_type, geo_json)

    def bbox(self) -> Mapping[str, str]:
        """Query for a bbox."""
        if not (isinstance(self.geometry, (tuple, list)) and len(self.geometry) == 4):
            raise InputTypeError("geometry", "tuple", BOX_ORD)

        geo_type = "esriGeometryEnvelope"
        geo_json = dict(zip(("xmin", "ymin", "xmax", "ymax"), self.geometry))
        return self._get_payload(geo_type, geo_json)

    def polygon(self) -> Mapping[str, str]:
        """Query for a polygon."""
        if not isinstance(self.geometry, sgeom.Polygon):
            raise InputTypeError("geometry", "Polygon")

        geo_type = "esriGeometryPolygon"
        geo_json = {"rings": [[[x, y] for x, y in zip(*self.geometry.exterior.coords.xy)]]}
        return self._get_payload(geo_type, geo_json)

    def polyline(self) -> Mapping[str, str]:
        """Query for a polyline."""
        if not isinstance(self.geometry, sgeom.LineString):
            raise InputTypeError("geometry", "LineString")

        geo_type = "esriGeometryPolyline"
        geo_json = {"paths": [[[x, y] for x, y in zip(*self.geometry.coords.xy)]]}
        return self._get_payload(geo_type, geo_json)


def match_crs(geom: G, in_crs: CRSTYPE, out_crs: CRSTYPE) -> G:
    """Reproject a geometry to another CRS.

    Parameters
    ----------
    geom : list or tuple or geometry
        Input geometry which could be a list of coordinates such as ``[(x1, y1), ...]``,
        a bounding box like so ``(xmin, ymin, xmax, ymax)``, or any valid ``shapely``'s
        geometry such as ``Polygon``, ``MultiPolygon``, etc..
    in_crs : str, int, or pyproj.CRS
        Spatial reference of the input geometry
    out_crs : str, int, or pyproj.CRS
        Target spatial reference

    Returns
    -------
    same type as the input geometry
        Transformed geometry in the target CRS.

    Examples
    --------
    >>> from pygeoogc.utils import match_crs
    >>> from shapely.geometry import Point
    >>> point = Point(-7766049.665, 5691929.739)
    >>> match_crs(point, "epsg:3857", "epsg:4326").xy
    (array('d', [-69.7636111130079]), array('d', [45.44549114818127]))
    >>> bbox = (-7766049.665, 5691929.739, -7763049.665, 5696929.739)
    >>> match_crs(bbox, "epsg:3857", "epsg:4326")
    (-69.7636111130079, 45.44549114818127, -69.73666165448431, 45.47699468552394)
    >>> coords = [(-7766049.665, 5691929.739)]
    >>> match_crs(coords, "epsg:3857", "epsg:4326")
    [(-69.7636111130079, 45.44549114818127)]
    """
    if pyproj.CRS(in_crs) == pyproj.CRS(out_crs):
        return geom

    project = pyproj.Transformer.from_crs(in_crs, out_crs, always_xy=True).transform

    if isinstance(
        geom,
        (
            sgeom.Polygon,
            sgeom.LineString,
            sgeom.MultiLineString,
            sgeom.MultiPolygon,
            sgeom.Point,
            sgeom.MultiPoint,
        ),
    ):
        return ops.transform(project, geom)  # type: ignore

    if isinstance(geom, tuple) and len(geom) == 4:
        return ops.transform(project, sgeom.box(*geom)).bounds  # type: ignore

    if isinstance(geom, list) and all(len(c) == 2 for c in geom):
        xx, yy = zip(*geom)
        return list(zip(*project(xx, yy)))

    gtypes = (
        "a list of coordinates such as [(x1, y1), ...],"
        + "a bounding box like so (xmin, ymin, xmax, ymax), or any valid shapely's geometry."
    )
    raise InputTypeError("geom", gtypes)


def check_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Check if an input inbox is a tuple of length 4."""
    if not (isinstance(bbox, tuple) and len(bbox) == 4):
        raise InputTypeError("bbox", "tuple", BOX_ORD)


def bbox_decompose(
    bbox: tuple[float, float, float, float],
    resolution: float,
    box_crs: CRSTYPE = 4326,
    max_px: int = 8000000,
) -> list[tuple[tuple[float, float, float, float], str, int, int]]:
    r"""Split the bounding box vertically for WMS requests.

    Parameters
    ----------
    bbox : tuple
        A bounding box; (west, south, east, north)
    resolution : float
        The target resolution for a WMS request in meters.
    box_crs : str, int, or pyproj.CRS, optional
        The spatial reference of the input bbox, default to ``epsg:4326``.
    max_px : int, opitonal
        The maximum allowable number of pixels (width x height) for a WMS requests,
        defaults to 8 million based on some trial-and-error.

    Returns
    -------
    list of tuples
        Each tuple includes the following elements:

        * Tuple of px_tot 4 that represents a bounding box (west, south, east, north) of a cell,
        * A label that represents cell ID starting from bottom-left to top-right, for example a
          2x2 decomposition has the following labels::

          |---------|---------|
          |         |         |
          |   0_1   |   1_1   |
          |         |         |
          |---------|---------|
          |         |         |
          |   0_0   |   1_0   |
          |         |         |
          |---------|---------|

        * Raster width of a cell,
        * Raster height of a cell.

    """
    check_bbox(bbox)

    geod = pyproj.Geod(ellps="GRS80")

    west, south, east, north = bbox

    xmin, ymin, xmax, ymax = match_crs(bbox, box_crs, 4326)

    x_dist = geod.geometry_length(sgeom.LineString([(xmin, ymin), (xmax, ymin)]))
    y_dist = geod.geometry_length(sgeom.LineString([(xmin, ymin), (xmin, ymax)]))
    width = int(math.ceil(x_dist / resolution))
    height = int(math.ceil(y_dist / resolution))

    if width * height <= max_px:
        bboxs = [(bbox, "0_0", width, height)]

    n_px = int(math.sqrt(max_px))

    def _split_directional(low: float, high: float, px_tot: int) -> tuple[list[int], list[float]]:
        npt = [n_px for _ in range(int(px_tot / n_px))] + [px_tot % n_px]
        xd = abs(high - low)
        dx = [xd * n / sum(npt) for n in npt]
        xs = [low + d for d in itertools.accumulate(dx)]
        xs.insert(0, low)
        return npt, xs

    nw, xs = _split_directional(west, east, width)
    nh, ys = _split_directional(south, north, height)

    bboxs = []
    for j in range(len(nh)):
        for i in range(len(nw)):
            bx_crs = (xs[i], ys[j], xs[i + 1], ys[j + 1])
            bboxs.append((bx_crs, f"{i}_{j}", nw[i], nh[j]))
    return bboxs


def validate_crs(crs: CRSTYPE) -> str:
    """Validate a CRS.

    Parameters
    ----------
    crs : str, int, or pyproj.CRS
        Input CRS.

    Returns
    -------
    str
        Validated CRS as a string.
    """
    try:
        return pyproj.CRS(crs).to_string().lower()  # type: ignore
    except pyproj.exceptions.CRSError as ex:
        raise InputTypeError("crs", "a valid CRS") from ex


def valid_wms_crs(url: str) -> list[str]:
    """Get valid CRSs from a WMS service version 1.3.0."""
    ns = "http://www.opengis.net/wms"

    def get_path(tag_list: list[str]) -> str:
        return f"/{{{ns}}}".join([""] + tag_list)[1:]

    kwds = {"params": {"service": "wms", "request": "GetCapabilities"}}
    root = ETree.fromstring(ar.retrieve_text([url], [kwds], ssl=False)[0])
    return [t.text.lower() for t in root.findall(get_path(["Capability", "Layer", "CRS"]))]
