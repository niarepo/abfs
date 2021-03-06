import numpy as np
import shapely.wkt
import matplotlib.pyplot as plt
import pandas as pd
from math import ceil
from PIL import Image
from skimage import color
from funcy import iffy, constantly, tap, rpartial
from toolz import memoize, curry, compose, pipe
from toolz.curried import map, juxt, mapcat, concatv
from toolz.sandbox.core import unzip
from geopandas import gpd
from osgeo import ogr, gdal, osr
from lenses import lens

from abfs.path import *
from abfs.constants import *
from abfs.group_data_split import GroupDataSplit, DEFAULT_SPLIT_CONFIG
from abfs.conversions import area_in_square_feet
from abfs.segmentation_augmentation import SegmentationAugmentation, MOVE_SCALE_ROTATE

list_unzip = compose(map(list), unzip)
list_concatv = compose(list, concatv)

BLACK = 0
BINARY_WHITE = 1
ALWAYS_TRUE = lambda df: df.index != -1

class Data():
    def __init__(self, config,
                 split_config=DEFAULT_SPLIT_CONFIG,
                 seg_aug_config=MOVE_SCALE_ROTATE,
                 batch_size=16,
                 override_df=None,
                 aug_random_seed=None,
                 augment=False):

        self.config = config
        self.split_config = split_config
        self._df = override_df
        self._image_ids = []
        self._data_filter = ALWAYS_TRUE
        self._split_data = None

        # Require batch size to be divisible by two if augmentation enabled
        assert augment == False or batch_size % 2 == 0

        self.batch_size = batch_size
        self.augment = augment
        self.augmentation = SegmentationAugmentation(seg_aug_config,
                                                     seed=aug_random_seed)

    # ============
    # General
    # ============

    @property
    def image_ids(self):
        if self._image_ids == []:
            self._image_ids = self.df.ImageId.unique()

        return self._image_ids

    def grouped_df(self):
        return self.df.groupby('ImageId')

    @property
    def df(self):
        if self._df is None:
            region_upper = self.config.region.upper()

            df = pd.read_csv(file_path(
                self.config, SUMMARY,
                f'{region_upper}_polygons_solution_{self.config.band}.csv'))

            geometry = [shapely.wkt.loads(x)
                        for x in df['PolygonWKT_Geo'].values]

            self._df = gpd.GeoDataFrame(df, crs={'init': 'epsg:4326'},
                                        geometry=geometry)

            self._df['sq_ft'] = self._df.geometry.apply(area_in_square_feet)

        return self._df[self.data_filter(self._df)]

    def __eq__(self, obj):
        return self._df.eq(obj._df).all().all()

    # =============================
    # Neural Network Input/Output
    # =============================

    def to_nn(self, shape, scale_pixels=False):
        """Convert data to neural network inputs/outputs
        """

        return pipe(
            self.df.ImageId.unique(),
            map(self._to_single_nn(shape)),
            list_unzip,
            iffy(constantly(self.augment), self._augment_nn),
            map(np.array),
            list,
            iffy(constantly(scale_pixels), lens[0].modify(lambda x: x / 255)),
            self._reshape_output
        )

    # ====================
    # Train/Val/Test Data
    # ====================

    def train_generator(self, klass, shape, **kwargs):
        def params():
            df = self.split_data.train_df()
            len_f = rpartial(self.train_batch_count, df)
            data_f = rpartial(self.train_batch_data, df)
            return len_f, data_f

        return klass(params, shape, **kwargs)

    def train_batch_data(self, batch_id, df=None):
        if df is None: df = self.split_data.train_df()
        return self._batch_data(df, self.augment, batch_id)

    def train_batch_count(self, df=None):
        if df is None: df = self.split_data.train_df()
        return self._batch_count(df, self.augment)


    def val_generator(self, klass, shape, **kwargs):
        return klass(lambda: (self.val_batch_count, self.val_batch_data),
                     shape, **kwargs)

    def val_batch_data(self, batch_id):
        df = self.split_data.val_df
        return self._batch_data(df, False, batch_id)

    def val_batch_count(self):
        return self._batch_count(self.split_data.val_df, False)


    def test_generator(self, klass, shape):
        return klass(lambda: (self.test_batch_count, self.test_batch_data),
                     shape)

    def test_batch_data(self, batch_id):
        df = self.split_data.test_df
        return self._batch_data(df, False, batch_id)

    def test_batch_count(self):
        return self._batch_count(self.split_data.test_df, False)

    @property
    def split_data(self):
        if self._split_data is None:
            self._split_data = GroupDataSplit(
                self.df, 'ImageId', self.split_config
            )

        return self._split_data

    # ==================
    # Data Filters
    # ==================

    @property
    def data_filter(self):
        return self._data_filter

    @data_filter.setter
    def data_filter(self, data_filter):
        self._data_filter = data_filter
        self._split_data = None

    def reset_filter(self):
        self.data_filter = ALWAYS_TRUE

    # ==================
    # Images / Masks
    # ==================

    def sample_image_predict(self, shape, predict_f, threshold=0.5):
        images, masks = self.to_nn(shape)

        input_data = images[0]
        input_image = input_data * 255
        truth_mask = np.squeeze(masks[0]).astype(np.int8)

        # Run prediction
        predicted = np.squeeze(predict_f(input_data))

        # Create mask of which are considered correct
        predicted_mask = np.zeros(predicted.shape, dtype=np.int8)
        predicted_mask[predicted[:, :] >= threshold] = 1

        # Create wrong mask that expresses confidence
        wrong_mask = np.logical_xor(truth_mask, predicted_mask)
        wrong = predicted * wrong_mask

        # Create correct mask that expresses confidence
        correct = predicted * np.logical_and(truth_mask, predicted_mask)

        false_negatives = np.logical_and(predicted_mask[:, :] == 0,
                                         wrong_mask[:, :] == 1)

        # Red = wrong, Green = correct, Blue = none
        pred_vs_truth_mask = np.dstack([
            wrong, correct, false_negatives
        ])

        # Create vibrancy overlay
        color_mask_hsv = color.rgb2hsv(pred_vs_truth_mask)
        image_hsv = color.rgb2hsv(input_image)
        image_hsv[..., 0] = color_mask_hsv[..., 0]
        image_hsv[..., 1] = color_mask_hsv[..., 1]

        return color.hsv2rgb(image_hsv) * 255


    def image_for(self, image_id):
        return plt.imread(image_id_to_path(self.config, image_id))

    def green_mask_for(self, image_id):
        mask = self.mask_for(image_id)
        blank = np.zeros(mask.shape)

        return np.dstack([blank, mask, blank])

    def mask_overlay_for(self, image_id):
        color_mask_hsv = color.rgb2hsv(self.green_mask_for(image_id))

        image_hsv = color.rgb2hsv(self.image_for(image_id))
        image_hsv[..., 0] = color_mask_hsv[..., 0]
        image_hsv[..., 1] = color_mask_hsv[..., 1] * 0.8

        return color.hsv2rgb(image_hsv)

    def mask_for(self, image_id):
        # Note: If certain parts of this method are extracted, a segmentation
        # fault occurs. See https://trac.osgeo.org/gdal/ticket/1936 and
        # https://trac.osgeo.org/gdal/wiki/PythonGotchas for why.

        # Get all rows with the given image id
        rows = self.grouped_df().get_group(image_id)

        # Determine output mask width and height
        srcRas_ds = gdal.Open(image_id_to_path(self.config, image_id))
        x_size = srcRas_ds.RasterXSize
        y_size = srcRas_ds.RasterYSize
        transform = srcRas_ds.GetGeoTransform()
        projection = srcRas_ds.GetProjection()

        # Create polygon layer
        polygon_ds = ogr.GetDriverByName('Memory').CreateDataSource('polygon')
        polygon_layer = polygon_ds.CreateLayer('poly', srs=None)

        # Create feature with all polygons
        feat = ogr.Feature(polygon_layer.GetLayerDefn())

        # Add all row polygons to multi-polygon
        multi_polygon = ogr.Geometry(ogr.wkbMultiPolygon)
        for _, row in rows.iterrows():
            geometry = ogr.CreateGeometryFromWkt(row.PolygonWKT_Geo)
            multi_polygon.AddGeometry(geometry)

        # Set multi-polygon geometry back on feature
        feat.SetGeometry(multi_polygon)

        # Set feature on polygon layer
        polygon_layer.SetFeature(feat)

        # Create raster layer of image size
        destination_layer = (gdal
                             .GetDriverByName('MEM')
                             .Create('', x_size, y_size, 1, gdal.GDT_Byte))

        # Match image transform and projection so that lat/long polygon
        # coordinates map to the proper location
        destination_layer.SetGeoTransform(transform)
        destination_layer.SetProjection(projection)

        # Set empty value of output mask to be black
        band = destination_layer.GetRasterBand(1)
        band.SetNoDataValue(BLACK)

        # Rasterize image with white polygon areas
        gdal.RasterizeLayer(destination_layer, [1], polygon_layer,
                            burn_values=[BINARY_WHITE])

        # Return image mask result as np.array
        return np.array(destination_layer.ReadAsArray())

    # ==================
    # Private Methods
    # ==================

    def _batch_data(self, df, augment, batch_id):
        batch_size = self.batch_size // (int(augment) + 1)

        start_index = batch_id * batch_size
        end_index = start_index + batch_size
        image_ids = df.ImageId.unique()[start_index:end_index]

        return Data(self.config,
                    override_df=df[df.ImageId.isin(image_ids)],
                    augment=augment)

    def _batch_count(self, df, augment):
        batch_size = self.batch_size // (int(augment) + 1)
        return ceil(df.ImageId.nunique() / batch_size)

    @curry
    def _to_single_nn(self, shape, image_id):
        return pipe(
            image_id,
            juxt(self.image_for, self.mask_for),
            map(self._resize_image(shape)),
            list
        )

    def _augment_nn(self, inputs_and_outputs):
        images, masks = inputs_and_outputs
        aug_images, aug_masks = self.augmentation.run(images, masks)

        return list_concatv(images, aug_images), list_concatv(masks, aug_masks)

    def _reshape_output(self, inputs_and_outputs):
        inputs, outputs = inputs_and_outputs
        return inputs, outputs[:, :, :, np.newaxis]

    @curry
    def _resize_image(self, shape, image):
        return np.array(Image.fromarray(image).resize(shape))

