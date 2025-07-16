from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingAlgorithm, 
    QgsProcessingParameterExtent, 
    QgsProcessingParameterString,
    QgsProcessingParameterEnum,
    QgsProcessingParameterVectorDestination,
    QgsCoordinateTransform, 
    QgsCoordinateReferenceSystem,
    QgsProject, 
    QgsProcessingException,
    QgsVectorLayer,
    QgsProcessingMultiStepFeedback,
    QgsApplication,
    QgsProcessingProvider
)
import os
import requests
import tempfile
import mercantile
import logging
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape
import inspect

LOG_PATH = os.path.join(os.path.dirname(__file__), 'ms_buildings_roads.log')
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

class MSBuildingsDownloaderAlgorithm(QgsProcessingAlgorithm):
    """Processing algorithm for downloading Microsoft Buildings/Roads data"""
    
    OUTPUT = 'OUTPUT'
    EXTENT = 'EXTENT'
    LOCATION = 'LOCATION'
    DATA_TYPE = 'DATA_TYPE'
    CSV_PATH = 'CSV_PATH'

    def __init__(self):
        super().__init__()
        self.dataset_links = None

    def initAlgorithm(self, config):
        """Initialize the algorithm parameters"""
        # Location selection - try to load locations from CSV
        location_options = ['Custom Area (use extent below)']
        try:
            self.dataset_links = pd.read_csv("https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv")
            locations = sorted(self.dataset_links['Location'].unique())
            location_options.extend(locations)
        except Exception as e:
            logging.warning(f"Could not load dataset from URL: {e}")
            
        self.addParameter(
            QgsProcessingParameterEnum(
                'LOCATION',
                'Select Location',
                options=location_options,
                allowMultiple=False,
                defaultValue=0
            )
        )
        
        self.addParameter(
            QgsProcessingParameterExtent(
                'EXTENT', 
                'Define extent (only required for custom area)',
                defaultValue=None,
                optional=True
            )
        )
        
        self.addParameter(
            QgsProcessingParameterEnum(
                'DATA_TYPE',
                'Data Type',
                options=['Buildings', 'Roads'],
                allowMultiple=False,
                defaultValue=0
            )
        )
        
        self.addParameter(
            QgsProcessingParameterString(
                'CSV_PATH',
                'Path to CSV manifest (optional - leave empty to use online version)',
                multiLine=False,
                optional=True,
                defaultValue=''
            )
        )
        
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Output Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        """Execute the algorithm"""
        
        # Setup feedback
        multi_step_feedback = QgsProcessingMultiStepFeedback(4, feedback)
        
        # Step 1: Load dataset
        multi_step_feedback.setCurrentStep(0)
        multi_step_feedback.pushInfo("Loading Microsoft Buildings dataset...")
        
        csv_path = self.parameterAsString(parameters, 'CSV_PATH', context)
        if csv_path:
            try:
                self.dataset_links = pd.read_csv(csv_path)
                feedback.pushInfo(f"Loaded dataset from local file: {csv_path}")
            except Exception as e:
                raise QgsProcessingException(f"Could not load CSV file: {str(e)}")
        elif self.dataset_links is None:
            try:
                self.dataset_links = pd.read_csv("https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv")
                feedback.pushInfo("Loaded dataset from online source")
            except Exception as e:
                raise QgsProcessingException(f"Could not load dataset: {str(e)}")
        
        # Step 2: Determine area of interest
        multi_step_feedback.setCurrentStep(1)
        multi_step_feedback.pushInfo("Determining area of interest...")
        
        location_index = self.parameterAsEnum(parameters, 'LOCATION', context)
        location_options = ['Custom Area (use extent below)']
        locations = sorted(self.dataset_links['Location'].unique())
        location_options.extend(locations)
        
        selected_location = location_options[location_index]
        
        if selected_location == 'Custom Area (use extent below)':
            # Use extent parameter - this is required for custom areas
            extent_param = parameters.get('EXTENT')
            if not extent_param:
                raise QgsProcessingException("Extent is required when using 'Custom Area'. Please define an extent or select a predefined location.")
            
            crs = self.parameterAsExtentCrs(parameters, "EXTENT", context)
            extent = self.parameterAsExtentGeometry(parameters, "EXTENT", context).boundingBox()
            
            # Transform to WGS84 if needed (following OpenTopography approach)
            if crs.authid() != "EPSG:4326":
                extent = QgsCoordinateTransform(
                    crs,
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance(),
                ).transformBoundingBox(extent)
            
            feedback.pushInfo(f"Using custom extent: {extent.yMinimum()}, {extent.xMinimum()}, {extent.yMaximum()}, {extent.xMaximum()}")
            
            # Get quadkeys for this extent
            quadkeys = self.get_quadkeys_for_bbox(
                extent.xMinimum(), extent.yMinimum(), 
                extent.xMaximum(), extent.yMaximum(), feedback
            )
            
            if not quadkeys:
                raise QgsProcessingException("No building data available for this extent")
            
            # Filter dataset for these quadkeys
            location_data = self.dataset_links[self.dataset_links['QuadKey'].isin(quadkeys)]
            
        else:
            # Use predefined location - extent is ignored
            feedback.pushInfo(f"Using predefined location: {selected_location} (extent parameter ignored)")
            location_data = self.dataset_links[self.dataset_links['Location'] == selected_location]
        
        if len(location_data) == 0:
            raise QgsProcessingException(f"No data found for the selected area")
        
        # Step 3: Download data
        multi_step_feedback.setCurrentStep(2)
        multi_step_feedback.pushInfo(f"Downloading {len(location_data)} data files...")
        
        all_gdfs = []
        for idx, (_, row) in enumerate(location_data.iterrows()):
            if feedback.isCanceled():
                break
                
            progress = int((idx / len(location_data)) * 100)
            multi_step_feedback.setProgress(progress)
            feedback.pushInfo(f"Downloading quadkey {row.QuadKey}...")
            
            try:
                # Download and process the file
                df = pd.read_json(row.Url, lines=True, compression='gzip')
                df['geometry'] = df['geometry'].apply(shape)
                gdf = gpd.GeoDataFrame(df, crs=4326)
                
                # Add metadata
                gdf['quadkey'] = row.QuadKey
                gdf['location'] = row.Location
                
                all_gdfs.append(gdf)
                
            except Exception as e:
                feedback.reportError(f"Failed to download {row.QuadKey}: {e}")
                continue
        
        if not all_gdfs:
            raise QgsProcessingException("No data could be downloaded")
        
        # Step 4: Process and save output
        multi_step_feedback.setCurrentStep(3)
        multi_step_feedback.pushInfo("Processing and saving output...")
        
        # Combine all GeoDataFrames
        combined_gdf = pd.concat(all_gdfs, ignore_index=True)
        
        # Get output path
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        
        # Save to output file
        combined_gdf.to_file(output_path, driver="GPKG")
        
        feedback.pushInfo(f"Successfully downloaded {len(combined_gdf)} features")
        
        return {self.OUTPUT: output_path}

    def get_quadkeys_for_bbox(self, west, south, east, north, feedback):
        """Get quadkeys that intersect with a bounding box using mercantile"""
        quadkeys = set()
        
        feedback.pushInfo(f"Computing quadkeys for bbox: {west}, {south}, {east}, {north}")
        
        # Use multiple zoom levels to get comprehensive coverage
        # Microsoft uses various zoom levels, typically 9-12
        for zoom_level in range(9, 13):
            try:
                # Use mercantile to get tiles that intersect the bounding box
                tiles = list(mercantile.tiles(west, south, east, north, zoom_level))
                
                feedback.pushInfo(f"Found {len(tiles)} tiles at zoom level {zoom_level}")
                
                # Convert tiles to quadkeys using mercantile
                for tile in tiles:
                    quadkey = mercantile.quadkey(tile)
                    quadkeys.add(quadkey)
                    
            except Exception as e:
                feedback.reportError(f"Error getting tiles for zoom {zoom_level}: {e}")
                continue
        
        feedback.pushInfo(f"Total unique quadkeys found: {len(quadkeys)}")
        return list(quadkeys)

    def name(self):
        return 'ms_buildings_downloader'

    def displayName(self):
        return self.tr('Microsoft Buildings/Roads Downloader')

    def group(self):
        return self.tr('Microsoft Data')

    def groupId(self):
        return 'microsoft_data'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)
        
    def icon(self):
        cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]
        icon_path = os.path.join(cmd_folder, 'icon.png')
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return QIcon()
        
    def shortHelpString(self):
        help_text = """
        This tool downloads Microsoft Buildings and Roads data for specified areas.
        
        You can either:
        1. Select a predefined location/country from the dropdown
        2. Define a custom extent using the map canvas
        
        The tool automatically downloads all relevant data files and combines them into a single layer.
        
        Data source: Microsoft Global Buildings dataset
        CSV manifest: https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv
        
        Note: Large areas may take significant time to download.
        """
        return self.tr(help_text)

    def createInstance(self):
        return MSBuildingsDownloaderAlgorithm()



# Processing Provider for the algorithm
class MSBuildingsProcessingProvider(QgsProcessingProvider):
    def __init__(self):
        QgsProcessingProvider.__init__(self)

    def id(self):
        return 'msbuildings'
        
    def name(self):
        return 'Microsoft Buildings & Roads'
        
    def icon(self):
        cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]
        icon_path = os.path.join(cmd_folder, 'icon.png')
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return QIcon()
        
    def loadAlgorithms(self):
        self.addAlgorithm(MSBuildingsDownloaderAlgorithm())

# Main plugin class
class MSBuildingsRoadsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        """Initialize the plugin GUI - only register processing provider"""
        self.provider = MSBuildingsProcessingProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        """Unload the plugin"""
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider) 