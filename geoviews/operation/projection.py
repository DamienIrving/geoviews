import logging

import param
import shapely
import numpy as np

from cartopy import crs as ccrs
from cartopy.img_transform import warp_array, _determine_bounds
from holoviews.core.data import MultiInterface
from holoviews.core.util import cartesian_product, get_param_values, max_extents, pd
from holoviews.operation import Operation
from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.collection import GeometryCollection

from ..data import GeoPandasInterface
from ..element import (Image, Shape, Polygons, Path, Points, Contours,
                       RGB, Graph, Nodes, EdgePaths, QuadMesh, VectorField,
                       HexTiles, Labels)
from ..util import (
    project_extents, geom_to_array, path_to_geom_dicts, polygons_to_geom_dicts,
    geom_dict_to_array_dict
)


class _project_operation(Operation):
    """
    Baseclass for projection operations, projecting elements from their
    source coordinate reference system to the supplied projection.
    """

    projection = param.ClassSelector(default=ccrs.GOOGLE_MERCATOR,
                                     class_=ccrs.Projection,
                                     instantiate=False, doc="""
        Projection the shape type is projected to.""")

    # Defines the types of elements supported by the operation
    supported_types = []

    def _process(self, element, key=None):
        return element.map(self._process_element, self.supported_types)


class project_path(_project_operation):
    """
    Projects Polygons and Path Elements from their source coordinate
    reference system to the supplied projection.
    """

    supported_types = [Polygons, Path, Contours, EdgePaths]

    def _process_element(self, element):
        if not len(element):
            return element.clone(crs=self.p.projection)

        crs = element.crs
        cylindrical = isinstance(crs, ccrs._CylindricalProjection)
        proj = self.p.projection
        if isinstance(proj, ccrs.CRS) and not isinstance(proj, ccrs.Projection):
            raise ValueError('invalid transform:'
                             ' Spherical contouring is not supported - '
                             ' consider using PlateCarree/RotatedPole.')

        boundary = Polygon(crs.boundary)
        bounds = [round(b, 10) for b in boundary.bounds]
        xoffset = round((boundary.bounds[2]-boundary.bounds[0])/2.)
        if isinstance(element, Polygons):
            geoms = polygons_to_geom_dicts(element, skip_invalid=False)
        else:
            geoms = path_to_geom_dicts(element, skip_invalid=False)

        data_bounds = max_extents([g['geometry'].bounds for g in geoms])
        total_bounds = tuple(round(b, 10) for b in data_bounds)

        projected = []
        for path in geoms:
            geom = path['geometry']
            if (cylindrical and total_bounds[0] >= (bounds[0]+xoffset) and
                total_bounds[2] > (bounds[2]+xoffset//2)):
                # Offset if lon and not centered on 0 longitude
                # i.e. lon_min > 0 and lon_max > 270
                geom = shapely.affinity.translate(geom, xoff=-xoffset)
            geom_bounds = [round(b, 10) for b in geom.bounds]

            if boundary and (geom_bounds[0] < bounds[0] or
                             geom_bounds[2] > bounds[2]):
                try:
                    geom = boundary.intersection(geom)
                except:
                    pass

            # Ensure minimum area for polygons (precision issues cause errors)
            if isinstance(geom, Polygon) and geom.area < 1e-15:
                continue
            elif isinstance(geom, MultiPolygon):
                polys = [g for g in geom if g.area > 1e-15]
                if not polys:
                    continue
                geom = MultiPolygon(polys)
            elif (not geom or isinstance(geom, GeometryCollection)):
                continue

            proj_geom = proj.project_geometry(geom, element.crs)

            # Attempt to fix geometry without being noisy about it
            logger = logging.getLogger()
            try:
                prev = logger.level
                logger.setLevel(logging.ERROR)
                if not proj_geom.is_valid:
                    proj_geom = proj.project_geometry(geom.buffer(0), element.crs)
            except:
                continue
            finally:
                logger.setLevel(prev)
            data = dict(path, geometry=proj_geom)
            projected.append(data)

        if len(geoms) and len(projected) == 0:
            self.warning('While projecting a %s element from a %s coordinate '
                         'reference system (crs) to a %s projection none of '
                         'the projected paths were contained within the bounds '
                         'specified by the projection. Ensure you have specified '
                         'the correct coordinate system for your data.' %
                         (type(element).__name__, type(element.crs).__name__,
                          type(self.p.projection).__name__))

        # Try casting back to original types
        if element.interface is GeoPandasInterface:
            import geopandas as gpd
            projected = gpd.GeoDataFrame(projected, columns=element.data.columns)
        elif element.interface is MultiInterface:
            x, y = element.kdims
            item = element.data[0] if element.data else None
            if item is None or (isinstance(item, dict) and 'geometry' in item):
                return element.clone(projected, crs=self.p.projection)
            projected = [geom_dict_to_array_dict(p, [x.name, y.name]) for p in projected]
            if any('holes' in p for p in projected):
                pass
            elif pd and isinstance(item, pd.DataFrame):
                projected = [pd.DataFrame(p, columns=item.columns) for p in projected]
            elif isinstance(item, np.ndarray):
                projected = [np.column_stack([p[d.name] for d in element.dimensions()])
                             for p in projected]
        return element.clone(projected, crs=self.p.projection)


class project_shape(_project_operation):
    """
    Projects Shape Element from the source coordinate reference system
    to the supplied projection.
    """

    supported_types = [Shape]

    def _process_element(self, element):
        if not len(element):
            return element.clone(crs=self.p.projection)
        geom = element.geom()
        vertices = geom_to_array(geom)
        if isinstance(geom, (MultiPolygon, Polygon)):
            obj = Polygons([vertices])
        else:
            obj = Path([vertices])
        geom = project_path(obj, projection=self.p.projection).geom()
        return element.clone(geom, crs=self.p.projection)


class project_points(_project_operation):

    supported_types = [Points, Nodes, VectorField, HexTiles, Labels]

    def _process_element(self, element):
        if not len(element):
            return element.clone(crs=self.p.projection)
        xdim, ydim = element.dimensions()[:2]
        xs, ys = (element.dimension_values(i) for i in range(2))
        coordinates = self.p.projection.transform_points(element.crs, xs, ys)
        mask = np.isfinite(coordinates[:, 0])
        new_data = {k: v[mask] for k, v in element.columns().items()}
        new_data[xdim.name] = coordinates[mask, 0]
        new_data[ydim.name] = coordinates[mask, 1]
        datatype = [element.interface.datatype]+element.datatype

        if len(new_data[xdim.name]) == 0:
            self.warning('While projecting a %s element from a %s coordinate '
                         'reference system (crs) to a %s projection none of '
                         'the projected paths were contained within the bounds '
                         'specified by the projection. Ensure you have specified '
                         'the correct coordinate system for your data.' %
                         (type(element).__name__, type(element.crs).__name__,
                          type(self.p.projection).__name__))

        return element.clone(tuple(new_data[d.name] for d in element.dimensions()),
                             crs=self.p.projection, datatype=datatype)



class project_graph(_project_operation):

    supported_types = [Graph]

    def _process_element(self, element):
        nodes = project_points(element.nodes, projection=self.projection)
        data = (element.data, nodes)
        if element._edgepaths:
            data = data + (project_path(element.edgepaths, projection=self.projection),)
        return element.clone(data, crs=self.projection)



class project_quadmesh(_project_operation):

    supported_types = [QuadMesh]

    def _process_element(self, element):
        proj = self.p.projection
        irregular = any(element.interface.irregular(element, kd)
                        for kd in element.kdims)
        zs = element.dimension_values(2, flat=False)
        if irregular:
            X, Y = [np.asarray(element.interface.coords(
                element, kd, expanded=True, edges=False))
                    for kd in element.kdims]
        else:
            X = element.interface.coords(element, 0, True, True, False)
            if np.all(X[0, 1:] < X[0, :-1]):
                X = X[:, ::-1]
            Y = element.interface.coords(element, 1, True, True, False)
            if np.all(Y[1:, 0] < Y[:-1, 0]):
                Y = Y[::-1, :]

        if X.shape != zs.shape:
            X = X[:-1] + np.diff(X, axis=0)/2.
            X = X[:, :-1] + (np.diff(X, axis=1)/2.)
        if Y.shape != zs.shape:
            Y = Y[:-1] + np.diff(Y, axis=0)/2.
            Y = Y[:, :-1] + (np.diff(Y, axis=1)/2.)

        coords = proj.transform_points(element.crs, X, Y)
        PX, PY = coords[..., 0], coords[..., 1]

        # Mask quads which are wrapping around the x-axis
        wrap_proj_types = (ccrs._RectangularProjection,
                           ccrs._WarpedRectangularProjection,
                           ccrs.InterruptedGoodeHomolosine,
                           ccrs.Mercator)
        if isinstance(proj, wrap_proj_types):
            with np.errstate(invalid='ignore'):
                edge_lengths = np.hypot(
                    np.diff(PX, axis=1),
                    np.diff(PY, axis=1)
                )
                to_mask = (
                    (edge_lengths >= abs(proj.x_limits[1] -
                                         proj.x_limits[0]) / 2) |
                    np.isnan(edge_lengths)
                )
            if np.any(to_mask):
                mask = np.zeros(zs.shape, dtype=np.bool)
                mask[:, 1:][to_mask] = True
                mask[:, 2:][to_mask[:, :-1]] = True
                mask[:, :-1][to_mask] = True
                mask[:, :-2][to_mask[:, 1:]] = True
                mask[1:, 1:][to_mask[:-1]] = True
                mask[1:, :-1][to_mask[:-1]] = True
                mask[:-1, 1:][to_mask[1:]] = True
                mask[:-1, :-1][to_mask[1:]] = True
                zs[mask] = np.NaN

        params = get_param_values(element)
        return QuadMesh((PX, PY, zs), crs=self.projection, **params)


class project_image(_project_operation):
    """
    Projects an geoviews Image to the specified projection,
    returning a regular HoloViews Image type. Works by
    regridding the data along projected bounds. Only supports
    rectangular projections.
    """

    fast = param.Boolean(default=False, doc="""
        Whether to enable fast reprojection with (much) better
        performance but poorer handling in polar regions.""")

    width = param.Integer(default=None, doc="""
        Width of the reprojectd Image""")

    height = param.Integer(default=None, doc="""
        Height of the reprojected Image""")

    link_inputs = param.Boolean(default=True, doc="""
        By default, the link_inputs parameter is set to True so that
        when applying project_image, backends that support linked streams
        update RangeXY streams on the inputs of the operation.""")

    supported_types = [Image, RGB]

    def _process(self, img, key=None):
        if self.p.fast:
            return self._fast_process(img, key)
        proj = self.p.projection
        x0, x1 = img.range(0)
        y0, y1 = img.range(1)
        xn, yn = img.interface.shape(img, gridded=True)[:2]
        px0, py0, px1, py1 = project_extents((x0, y0, x1, y1),
                                             img.crs, proj)
        src_ext, trgt_ext = (x0, x1, y0, y1), (px0, px1, py0, py1)
        arrays = []
        for vd in img.vdims:
            arr = img.dimension_values(vd, flat=False)
            if arr.size:
                projected, extents = warp_array(arr, proj, img.crs, (xn, yn),
                                                src_ext, trgt_ext)
            else:
                projected, extents = arr, trgt_ext
            arrays.append(projected)
        xunit = ((extents[1]-extents[0])/float(xn))/2.
        yunit = ((extents[3]-extents[2])/float(yn))/2.
        xs = np.linspace(extents[0]+xunit, extents[1]-xunit, xn)
        ys = np.linspace(extents[2]+yunit, extents[3]-yunit, yn)
        return img.clone((xs, ys)+tuple(arrays), bounds=None, kdims=img.kdims,
                         vdims=img.vdims, crs=proj, xdensity=None,
                         ydensity=None)

    def _fast_process(self, element, key=None):
        # Project coordinates
        proj = self.p.projection
        if proj == element.crs:
            return element

        h, w = element.interface.shape(element, gridded=True)[:2]
        xs = element.dimension_values(0)
        ys = element.dimension_values(1)
        if isinstance(element, RGB):
            rgb = element.rgb
            array = np.dstack([np.flipud(rgb.dimension_values(d, flat=False))
                               for d in rgb.vdims])
        else:
            array = element.dimension_values(2, flat=False)

        (x0, y0, x1, y1) = element.bounds.lbrt()
        width = int(w) if self.p.width is None else self.p.width
        height = int(h) if self.p.height is None else self.p.height

        bounds = _determine_bounds(xs, ys, element.crs)
        yb = bounds['y']
        resampled = []
        xvalues = []
        for xb in bounds['x']:
            px0, py0, px1, py1 = project_extents((xb[0], yb[0], xb[1], yb[1]), element.crs, proj)
            if len(bounds['x']) > 1:
                xfraction = (xb[1]-xb[0])/(x1-x0)
                fraction_width = int(width*xfraction)
            else:
                fraction_width = width
            xs = np.linspace(px0, px1, fraction_width)
            ys = np.linspace(py0, py1, height)
            cxs, cys = cartesian_product([xs, ys])

            pxs, pys, _ = element.crs.transform_points(proj, np.asarray(cxs), np.asarray(cys)).T
            icxs = (((pxs-x0) / (x1-x0)) * w).astype(int)
            icys = (((pys-y0) / (y1-y0)) * h).astype(int)
            xvalues.append(xs)

            icxs[icxs<0] = 0
            icys[icys<0] = 0
            icxs[icxs>=w] = w-1
            icys[icys>=h] = h-1
            resampled_arr = array[icys, icxs]
            if isinstance(element, RGB):
                nvdims = len(element.vdims)
                resampled_arr = resampled_arr.reshape((fraction_width, height, nvdims)).transpose([1, 0, 2])
            else:
                resampled_arr = resampled_arr.reshape((fraction_width, height)).T
            resampled.append(resampled_arr)
        xs = np.concatenate(xvalues[::-1])
        resampled = np.hstack(resampled[::-1])
        datatypes = [element.interface.datatype, 'xarray', 'grid']
        data = (xs, ys)
        for i in range(len(element.vdims)):
            if resampled.ndim > 2:
                data = data + (resampled[::-1, :, i],)
            else:
                data = data + (resampled,)
        return element.clone(data, crs=proj, bounds=None, datatype=datatypes)


class project(Operation):
    """
    Projects GeoViews Element types to the specified projection.
    """

    projection = param.ClassSelector(default=ccrs.GOOGLE_MERCATOR,
                                     class_=ccrs.Projection,
                                     instantiate=False, doc="""
        Projection the image type is projected to.""")

    _operations = [project_path, project_image, project_shape,
                   project_graph, project_quadmesh, project_points]

    def _process(self, element, key=None):
        for op in self._operations:
            element = element.map(op.instance(projection=self.p.projection),
                                  op.supported_types)
        return element
