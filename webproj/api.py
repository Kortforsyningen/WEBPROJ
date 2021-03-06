import os
import json
from pathlib import Path

from flask import Flask
from flask_cors import CORS
from flask_restx import Api, Resource, fields, abort
import pyproj
from pyproj.transformer import Transformer, AreaOfInterest

from webproj.utils import IntFloatConverter

version = "1.1.0"

if "WEBPROJ_LIB" in os.environ:
    pyproj.datadir.append_data_dir(os.environ["WEBPROJ_LIB"])

# Set up the app
app = Flask(__name__)
app.url_map.converters["number"] = IntFloatConverter
CORS(app)

api = Api(app, version=version, title="WEBPROJ")

_DATA = Path(__file__).parent / Path("data.json")

with open(_DATA, "r", encoding="UTF-8") as data:
    CRS_LIST = json.load(data)

AOI = {
    "DK": AreaOfInterest(3.0, 54.5, 15.5, 58.0),
    "GL": AreaOfInterest(-75.0, 56.0, 8.5, 87.5),
}


def _make_4d(coord):
    if len(coord) == 2:
        return (coord[0], coord[1], None, None)

    if len(coord) == 3:
        return (coord[0], coord[1], coord[2], None)

    if len(coord) == 4:
        return (coord[0], coord[1], coord[2], coord[3])

    return ()


class OptimusPrime:
    """
    Optimus Prime is a Transformer... also, this is fun and avoids
    name clashes with pyproj
    """

    def __init__(self, src, dst):
        """
        Transformation from src to dst
        """
        self.pre_pipeline = None
        self.epsg_pipeline = None
        self.post_pipeline = None

        src = src.upper()
        dst = dst.upper()
        dst_hub = dst

        if src not in CRS_LIST.keys():
            raise ValueError(f"Unknown source CRS identifier: '{src}'")

        if dst not in CRS_LIST.keys():
            raise ValueError(f"Unknown destination CRS identifier: '{dst}'")

        src_region = CRS_LIST[src]["country"]
        dst_region = CRS_LIST[dst]["country"]
        if src_region != dst_region and "Global" not in (src_region, dst_region):
            raise ValueError("CRS's are not compatible across countries")

        # determine region of transformation
        if src_region == dst_region:
            region = AOI[src_region]
        elif src_region == "Global":
            region = AOI[dst_region]
        else:
            region = AOI[src_region]

        src_auth = src.split(":")[0]
        dst_auth = dst.split(":")[0]

        # determine which transformation stops to do along the way
        non_epsg_src = src_auth != "EPSG"
        non_epsg_dst = dst_auth != "EPSG"

        if non_epsg_src:
            pipeline = (
                f"+proj=pipeline "
                f"+step +inv +init={src} "
                f"+step +proj=unitconvert +xy_in=rad +xy_out=deg "
                f"+step +proj=axisswap +order=2,1"
            )
            self.pre_pipeline = Transformer.from_pipeline(pipeline)

            if src_auth == "DK":
                src = "EPSG:4258"

        # standard case, which handles all transformations between
        # CRS's that are both EPSG SRID's AND which handles transformations
        # where ONE of the two CRS's is a non-EPSG SRID by supplying a
        # transformation hub using ETRS89 or GR96
        if src != dst or non_epsg_src != non_epsg_dst:
            if dst_auth == "DK":
                dst_hub = "EPSG:4258"
            if dst_auth == "GL":
                dst_hub = "EPSG:4909"

            try:
                self.epsg_pipeline = Transformer.from_crs(
                    src, dst_hub, area_of_interest=region
                )
            except RuntimeError as e:
                raise ValueError("Invalid CRS identifier")

        if non_epsg_dst:
            pipeline = (
                f"+proj=pipeline "
                f"+step +proj=axisswap +order=2,1 "
                f"+step +proj=unitconvert +xy_in=deg +xy_out=rad "
                f"+step +init={dst}"
            )
            self.post_pipeline = Transformer.from_pipeline(pipeline)

    def transform(self, coord):
        """
        Transform coordinate
        """
        (v1, v2, v3, v4) = coord
        if self.pre_pipeline:
            out = self.pre_pipeline.transform(v1, v2, v3, v4)
            (v1, v2, v3, v4) = _make_4d(out)

        if self.epsg_pipeline:
            out = self.epsg_pipeline.transform(v1, v2, v3, v4)
            (v1, v2, v3, v4) = _make_4d(out)

        if self.post_pipeline:
            out = self.post_pipeline.transform(v1, v2, v3, v4)
            (v1, v2, v3, v4) = _make_4d(out)

        if float("inf") in out or float("-inf") in out:
            raise ValueError(
                "Input coordinate outside area of use of either source or destination CRS"
            )

        return (v1, v2, v3, v4)


class TransformerFactory:
    transformers = {}

    @classmethod
    def create(cls, src, dst):
        if src not in cls.transformers.keys():
            cls.transformers[src] = {}

        if dst not in cls.transformers[src].keys():
            cls.transformers[src][dst] = OptimusPrime(src, dst)

        return cls.transformers[src][dst]


@api.route("/")
class EndPoint(Resource):
    def get(self):
        return {}


@api.route("/v1.0/crs/")
@api.route("/v1.1/crs/")
class CRSIndex(Resource):
    def get(self):
        """
        List available coordinate reference systems
        """
        index = {}
        for srid, crsinfo in CRS_LIST.items():
            if crsinfo["country"] not in index:
                index[crsinfo["country"]] = []
            index[crsinfo["country"]].append(srid)

        return index


@api.route("/v1.0/crs/<string:crs>")
@api.route("/v1.1/crs/<string:crs>")
class CRS(Resource):
    def get(self, crs):
        """
        Retrieve information about a given coordinate reference system
        """
        try:
            return CRS_LIST[crs.upper()]
        except KeyError:
            abort(404, message=f"'{crs}' not available")


@api.route("/v1.0/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>")
@api.route("/v1.1/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>")
class Transformation2D(Resource):
    doc = {
        "src": "Source CRS",
        "dst": "Destination CRS",
        "v1": "1st coordinate component",
        "v2": "2nd coordinate component",
    }

    @api.doc(params=doc)
    def get(self, src, dst, v1, v2):
        """
        Transform a 2D coordinate from one CRS to another
        """
        try:
            transformer = TransformerFactory.create(src, dst)
            (v1, v2, v3, v4) = transformer.transform(_make_4d((v1, v2)))
        except ValueError as error:
            abort(404, message=error)

        return {"v1": v1, "v2": v2, "v3": v3, "v4": v4}


@api.route("/v1.0/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>,<number:v3>")
@api.route("/v1.1/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>,<number:v3>")
class Transformation3D(Resource):
    doc = {
        "src": "Source CRS",
        "dst": "Destination CRS",
        "v1": "1st coordinate component",
        "v2": "2nd coordinate component",
        "v3": "3rd coordinate component",
    }

    @api.doc(params=doc)
    def get(self, src, dst, v1, v2, v3):
        """
        Transform a 3D coordinate from one CRS to another
        """
        try:
            transformer = TransformerFactory.create(src, dst)
            (v1, v2, v3, v4) = transformer.transform(_make_4d((v1, v2, v3)))
        except ValueError as error:
            abort(404, message=error)

        return {"v1": v1, "v2": v2, "v3": v3, "v4": v4}


@api.route(
    "/v1.0/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>,<number:v3>,<number:v4>"
)
@api.route(
    "/v1.1/trans/<string:src>/<string:dst>/<number:v1>,<number:v2>,<number:v3>,<number:v4>"
)
class Transformation4D(Resource):
    doc = {
        "src": "Source CRS",
        "dst": "Destination CRS",
        "v1": "1st coordinate component",
        "v2": "2nd coordinate component",
        "v3": "3rd coordinate component",
        "v4": "4th coordinate component",
    }

    @api.doc(params=doc)
    def get(self, src, dst, v1, v2, v3=None, v4=None):
        """
        Transform a 4D coordinate from one CRS to another
        """
        try:
            transformer = TransformerFactory.create(src, dst)
            (v1, v2, v3, v4) = transformer.transform((v1, v2, v3, v4))
        except ValueError as error:
            abort(404, message=error)

        return {"v1": v1, "v2": v2, "v3": v3, "v4": v4}
