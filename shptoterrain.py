# -*- coding: utf-8 -*-

import os
from forge.terrain import TerrainTile
from forge.terrain.topology import TerrainTopology
from forge.lib.shapefile_utils import ShpToGDALFeatures

basename = 'single_wgs84'
directory = '.tmp'
extension = '.terrain'

curDir = os.getcwd()
filePathSource = '/Users/liberostelios/Downloads/test_area/%s.shp' % (basename)
filePathTarget = '%s/%s%s' % (directory, basename, extension)

shapefile = ShpToGDALFeatures(shpFilePath=filePathSource)
features = shapefile.__read__()

terrainTopo = TerrainTopology(features=features)
terrainTopo.fromGDALFeatures()
terrainFormat = TerrainTile()

terrainFormat.fromTerrainTopology(terrainTopo)
terrainFormat.toFile(filePathTarget)

# Display SwissCoordinates
terrainFormat.computeVerticesCoordinates(epsg=7415)
print terrainFormat
