# -*- coding: utf-8 -*-

import os
import time
import datetime
import ConfigParser
import multiprocessing
from sqlalchemy.sql import and_
from sqlalchemy.orm.exc import NoResultFound
from geoalchemy2 import WKBElement
from geoalchemy2.shape import to_shape

import forge.lib.cartesian2d as c2d
from forge.db import DB
from forge.terrain import TerrainTile
from forge.terrain.metadata import TerrainMetadata
from forge.terrain.topology import TerrainTopology
from forge.models.tables import modelsPyramid
from forge.lib.tiles import TerrainTiles, QueueTerrainTiles
# from forge.lib.boto_conn import getBucket, writeToS3, getSQS, writeSQSMessage
from forge.lib.helpers import gzipFileObject, timestamp, transformCoordinate, createBBox
from forge.lib.global_geodetic import GlobalGeodetic
from forge.lib.geometry_processors import processRingCoordinates
from forge.lib.logs import getLogger
from forge.lib.poolmanager import PoolManager


# Init logging
loggingConfig = ConfigParser.RawConfigParser()
loggingConfig.read('logging.cfg')
logger = getLogger(loggingConfig, __name__, suffix=timestamp())


# shared counter
tilecount = multiprocessing.Value('i', 0)
skipcount = multiprocessing.Value('i', 0)

visibility_timeout = 3600


def createTileFromQueue(tq):
    pid = os.getpid()
    try:
        (qName, t0, dbConfigFile, hasLighting, hasWatermask) = tq
        sqs = getSQS()
        q = sqs.get_queue(qName)
        geodetic = GlobalGeodetic(True)
        # we do this as long as we are finding messages in the queue
        while True:
            parseOk = True
            try:
                # 20 is maximum wait time
                m = q.read(
                    visibility_timeout = visibility_timeout,
                    wait_time_seconds = 20
                )
                if m is None:
                    logger.info(
                        '[%s] No more messages found. Closing process' % pid)
                    break
                body = m.get_body()
                tiles = map(int, body.split(','))
            except Exception as e:
                parseOk = False

            if not parseOk or len(tiles) % 3 != 0:
                logger.warning(
                    '[%s] Unparsable message received.'
                    'Skipping...and removing message [%s]' % (pid, m.get_body())
                )
                q.delete_message(m)
                continue

            for i in range(0, len(tiles), 3):
                try:
                    tileXYZ = [tiles[i], tiles[i + 1], tiles[i + 2]]
                    tilebounds = geodetic.TileBounds(
                        tileXYZ[0], tileXYZ[1], tileXYZ[2]
                    )
                    createTile(
                        (tilebounds, tileXYZ, t0, dbConfigFile,
                         hasLighting, hasWatermask)
                    )
                except Exception as e:
                    logger.error('[%s] Error while processing '
                        'specific tile %s' % (pid, str(e)), exc_info=True)

            # when successfull, we delete the message from the queue
            logger.info('[%s] Successfully treated an SQS message: %s' % (
                pid, body))
            q.delete_message(m)
    except Exception as e:
        logger.error('[%s] Error occured during processing. '
            'Halting process ' % str(e), exc_info=True)


def createTile(tile):
    session = None
    pid = os.getpid()

    try:
        (bounds, tileXYZ, t0, dbConfigFile, bucketBasePath,
            hasLighting, hasWatermask) = tile

        db = DB(dbConfigFile)
        with db.userSession() as session:
            # bucket = getBucket()

            # Get the model according to the zoom level
            model = modelsPyramid.getModelByZoom(tileXYZ[2])

            watermask = []
            if hasWatermask:
                lakeModel = modelsPyramid.getLakeModelByZoom(tileXYZ[2])
                query = session.query(
                    lakeModel.watermaskRasterize(bounds).label('watermask')
                )
                for q in query:
                    watermask = q.watermask

            # Get the interpolated point at the 4 corners
            # 0: (minX, minY), 1: (minX, maxY), 2: (maxX, maxY), 3: (maxX, minY)
            pts = [
                (bounds[0], bounds[1], 0),
                (bounds[0], bounds[3], 0),
                (bounds[2], bounds[3], 0),
                (bounds[2], bounds[1], 0)
            ]

            def toSubQuery(x):
                return session.query(
                    model.id, model.interpolateHeightOnPlane(pts[x])
                ).filter(
                    and_(
                        model.bboxIntersects(createBBox(pts[x], 0.01)),
                        model.pointIntersects(pts[x])
                    )
                ).subquery('p%s' % x)
            subqueries = [toSubQuery(i) for i in range(0, len(pts))]

            # Get the height of the corner points as postgis cannot properly
            # clip a polygon
            cornerPts = {}
            step = 2
            j = step
            query = session.query(*subqueries)
            for q in query:
                for i in range(0, len(q), step):
                    sub = q[i:j]
                    j += step
                    cornerPts[sub[0]] = list(
                        to_shape(WKBElement(sub[1])).coords
                    )

            # Clip using the bounds
            clippedGeometry = model.bboxClippedGeom(bounds)
            query = session.query(
                model.id,
                clippedGeometry.label('clip')
            ).filter(model.bboxIntersects(bounds))

            terrainTopo = TerrainTopology(hasLighting=hasLighting)
            for q in query:
                if to_shape(q.clip).geom_type == "GeometryCollection":
                    continue
                if to_shape(q.clip).geom_type == "LineString" or to_shape(q.clip).geom_type == "Point":
                    continue
                coords = list(to_shape(q.clip).exterior.coords)
                if q.id in cornerPts:
                    pt = cornerPts[q.id][0]
                    for i in range(0, len(coords)):
                        c = coords[i]
                        if c[0] == pt[0] and c[1] == pt[1]:
                            coords[i] = [c[0], c[1], pt[2]]

                try:
                    rings = processRingCoordinates(coords)
                except Exception as e:
                    msg = '[%s] --------- ERROR ------- occured while ' \
                        'collapsing non triangular shapes\n' % pid
                    msg += '[%s]: %s' % (pid, e)
                    logger.error(msg, exc_info=True)
                    raise Exception(e)
                # Redundant coord has been remove already
                for vertices in rings:
                    terrainTopo.addVertices(vertices)

            baseDir = '/Users/liberostelios/3d_forge_geodata/output'
            terrainDir = '%s/%s/%s' % (baseDir,
                tileXYZ[2], tileXYZ[0])
            bucketKey = '%s/%s.terrain' % (
                terrainDir, tileXYZ[1])
            verticesLength = len(terrainTopo.vertices)
            if verticesLength > 0:
                terrainTopo.create()
                # Prepare terrain tile
                terrainFormat = TerrainTile(watermask=watermask)
                terrainFormat.fromTerrainTopology(terrainTopo, bounds=bounds)

                # STELIOS: We want this in a file, instead
                
                if not os.path.isdir(terrainDir):
                    os.makedirs(terrainDir)
                terrainFormat.toFile(bucketKey)

                logger.info('[%s] Just did %s %s' % (
                        pid, bucketKey, bounds
                    )
                )

                # Bytes manipulation and compression
                # fileObject = terrainFormat.toStringIO()
                # compressedFile = gzipFileObject(fileObject)
                # writeToS3(
                #     bucket, bucketKey, compressedFile, model.__tablename__,
                #     bucketBasePath, contentType=terrainFormat.getContentType()
                # )
                tend = time.time()
                tilecount.value += 1
                val = tilecount.value
                total = val + skipcount.value
                if val % 10 == 0:
                    logger.info('[%s] Last tile %s (%s rings). '
                        '%s to write %s tiles. (total processed: %s)' % (
                            pid, bucketKey, verticesLength,
                            str(datetime.timedelta(seconds=tend - t0)),
                            val, total
                        )
                    )

            else:
                skipcount.value += 1
                val = skipcount.value
                total = val + tilecount.value
                # One should write an empyt tile
                logger.info('[%s] Skipping %s %s because no features found '
                    'for this tile (%s skipped from %s total)' % (
                        pid, bucketKey, bounds, val, total
                    )
                )
    except Exception as e:
        logger.error(e, exc_info=True)
        raise Exception(e)
    finally:
        db.userEngine.dispose()

    return 0


def scanTerrain(tMeta, tile, session, tilecount):
    try:
        (bounds, tileXYZ, t0, dbConfigFile, bucketBasePath, hasLighting, hasWatermask) = tile

        # Get the model according to the zoom level
        model = modelsPyramid.getModelByZoom(tileXYZ[2])
        query = session.query(model.id).filter(
            model.bboxIntersects(bounds)
        ).limit(1)
        try:
            query.one()
        except NoResultFound as e:
            tMeta.removeTile(tileXYZ[0], tileXYZ[1], tileXYZ[2])
    except Exception as e:
        logger.error(e, exc_info=True)
        raise Exception(e)

    tend = time.time()
    if tilecount % 1000 == 0:
        logger.info('It took %s to scan %s tiles' % (
            str(datetime.timedelta(seconds=tend - t0)), tilecount))

    return tMeta


class TilerManager:

    def __init__(self, dbConfigFile, tmsConfigFile):
        self.dbConfigFile = dbConfigFile

        tmsConfig = ConfigParser.RawConfigParser()
        tmsConfig.read(tmsConfigFile)
        self.tmsConfig = tmsConfig

    def create(self):
        self.t0 = time.time()

        tilecount.value = 0
        skipcount.value = 0

        tiles = TerrainTiles(self.dbConfigFile, self.tmsConfig, self.t0)
        procfactor = int(self.tmsConfig.get('General', 'procfactor'))

        pm = PoolManager(logger=logger, factor=procfactor)

        maxChunks = int(self.tmsConfig.get('General', 'maxChunks'))

        nbTiles = self.numOfTiles()
        tilesPerProc = int(nbTiles / pm.numOfProcesses())
        if tilesPerProc < maxChunks:
            maxChunks = tilesPerProc
        if maxChunks < 1:
            maxChunks = 1

        logger.info('Starting creation of %s tiles (%s per chunk)' % (
            nbTiles, maxChunks))
        pm.process(tiles, createTile, maxChunks)

        tend = time.time()
        logger.info('It took %s to create %s tiles (%s were skipped)' % (
            str(datetime.timedelta(seconds=tend - self.t0)), tilecount.value,
            skipcount.value
        ))

    # Create AWS sqs queue with all the tiles to create
    # based on current configuration as well as meta data
    def createQueue(self):
        queueName = self.tmsConfig.get('General', 'sqsqueue')
        maxChunks = int(self.tmsConfig.get('General', 'maxChunks'))
        self.t0 = time.time()
        if len(queueName) <= 0:
            logger.error('Missing queueName')
            return
        try:
            sqs = getSQS()
            q = sqs.get_queue(queueName)
            if q is not None:
                logger.error('Queue already exists. Can\'t overwrite'
                    ' existing queue. [%s]' % (queueName))
                return

            # queue with default message visiblity time of 3600. So each message is
            # blocked for other users for 3600 seconds. Default would be 30 seconds.
            # It's that high because each message contains maxChunks tiles
            q = sqs.create_queue(queueName, visibility_timeout = visibility_timeout)
            # Assure queue is kept for maximum of 14 weeks (aws limit).
            # default would be 4 days.
            sqs.set_queue_attribute(q, 'MessageRetentionPeriod', 1209600)
        except Exception as e:
            logger.error('Error during creation of queue:\n' + str(e), exc_info=True)
            return

        if q.count() > 0:
            logger.error('Queue already contains messages. '
                'Use a different queue or delete this queue first')
            return

        logger.info('Queue ' + queueName + ' has been created')
        tiles = TerrainTiles(self.dbConfigFile, self.tmsConfig, self.t0)
        nbTiles = self.numOfTiles()
        try:
            logger.info('Starting creation of SQS queue with approx. '
                '%s tiles)' % (nbTiles))
            totalcount = 0
            tcount = 0
            messagecount = 0
            msg = ''
            for tile in tiles:
                (bounds, tileXYZ, t0, dbConfigFile,
                 hasLighting, hasWatermask) = tile
                if msg:
                    msg += ','
                msg += ('%s,%s,%s' % (
                    str(tileXYZ[0]), str(tileXYZ[1]), str(tileXYZ[2])
                ))
                tcount += 1
                if tcount >= maxChunks:
                    messagecount += 1
                    writeSQSMessage(q, msg)
                    tcount = 0
                    msg = ''
                totalcount = totalcount + 1
            if msg:
                messagecount += 1
                writeSQSMessage(q, msg)
        except Exception as e:
            logger.error('Error during writing of sqs message:\n' + str(e),
                exc_info=True)

        tend = time.time()
        logger.info('It took %s to create %s message in SQS gueue '
            'representing %s tiles' % (
                str(datetime.timedelta(seconds=tend - self.t0)),
                messagecount, totalcount
            )
        )

    # To delete an existing queue. Be very carefull...alot of info
    # will be deleted potentially
    def deleteQueue(self):
        queueName = self.tmsConfig.get('General', 'sqsqueue')
        if len(queueName) <= 0:
            logger.error('Missing queueName')
            return
        try:
            sqs = getSQS()
            q = sqs.get_queue(queueName)
            sqs.delete_queue(q)
        except Exception as e:
            logger.error('Error during deletion of queue:\n' + str(e), exc_info=True)
            return

    # Create tiles based on given Queue
    def createTiles(self):
        tilecount.value = 0
        skipcount.value = 0
        queueName = self.tmsConfig.get('General', 'sqsqueue')
        self.t0 = time.time()
        if len(queueName) <= 0:
            logger.error('Missing queueName')
            return
        procfactor = int(self.tmsConfig.get('General', 'procfactor'))

        pm = PoolManager(logger=logger, factor=procfactor)
        qtiles = QueueTerrainTiles(
            queueName, self.dbConfigFile, self.tmsConfig, self.t0, pm.numOfProcesses()
        )

        logger.info('Starting creation of tiles from queue %s ' % (queueName))
        pm.process(qtiles, createTileFromQueue, 1)
        tend = time.time()
        logger.info('It took %s to create %s tiles (%s were skipped) from queue' % (
            str(datetime.timedelta(seconds=tend - self.t0)), tilecount.value,
            skipcount.value
        ))

    def queueStats(self):
        queueName = self.tmsConfig.get('General', 'sqsqueue')
        if len(queueName) <= 0:
            logger.error('Missing queueName')
            return
        try:
            sqs = getSQS()
            q = sqs.get_queue(queueName)
            attrs = sqs.get_queue_attributes(q)
        except Exception as e:
            logger.error('Error during statistics collection:\n' + str(e), exc_info=True)
            return
        logger.info(attrs)

    def metadata(self):
        t0 = time.time()
        serverAddress = self.tmsConfig.get('General', 'serveraddress')
        basePath = self.tmsConfig.get('General', 'bucketpath')
        baseUrls = [
            serverAddress + basePath + "{z}/{x}/{y}.terrain?v={version}"
        ]

        db = DB('configs/terrain/database.cfg')
        tiles = TerrainTiles(self.dbConfigFile, self.tmsConfig, t0)
        tMeta = TerrainMetadata(
            bounds=tiles.bounds, minzoom=tiles.tileMinZ, maxzoom=tiles.tileMaxZ,
            useGlobalTiles=True, hasLighting=tiles.hasLighting,
            hasWatermask=tiles.hasWatermask, baseUrls=baseUrls)

        try:
            with db.userSession() as session:
                tilecount = 1
                for tile in tiles:
                    tMeta = scanTerrain(tMeta, tile, session, tilecount)
                    tilecount += 1

                tend = time.time()
                logger.info('It took %s to scan %s tiles' % (
                    str(datetime.timedelta(seconds=tend - t0)), tilecount))
        except Exception as e:
            logger.error('An error occured during layer.json creation')
            logger.error('%s' % e, exc_info=True)
            raise Exception(e)
        finally:
            db.userEngine.dispose()

        with open('.tmp/layer.json', 'w') as f:
            f.write(tMeta.toJSON())

    def _stats(self, withDb=True):
        self.t0 = time.time()
        total = 0

        msg = '\n'
        tiles = TerrainTiles(self.dbConfigFile, self.tmsConfig, self.t0)
        geodetic = GlobalGeodetic(True)
        bounds = (tiles.minLon, tiles.minLat, tiles.maxLon, tiles.maxLat)
        zooms = range(tiles.tileMinZ, tiles.tileMaxZ + 1)

        db = DB('configs/terrain/database.cfg')
        try:
            with db.userSession() as session:
                for i in xrange(0, len(zooms)):
                    zoom = zooms[i]
                    model = modelsPyramid.getModelByZoom(zoom)
                    nbObjects = None
                    if withDb:
                        nbObjects = session.query(model).filter(
                            model.bboxIntersects(bounds)
                        ).count()
                    tileMinX, tileMinY = geodetic.LonLatToTile(
                        bounds[0], bounds[1], zoom
                    )
                    tileMaxX, tileMaxY = geodetic.LonLatToTile(
                        bounds[2], bounds[3], zoom
                    )
                    # Fast approach, but might not be fully correct
                    if tiles.fullonly == 1:
                        tileMinX += 1
                        tileMinY += 1
                        tileMaxX -= 1
                        tileMaxY -= 1
                    tileBounds = geodetic.TileBounds(tileMinX, tileMinY, zoom)
                    xCount = tileMaxX - tileMinX + 1
                    yCount = tileMaxY - tileMinY + 1
                    nbTiles = xCount * yCount
                    total += nbTiles
                    pointA = transformCoordinate('POINT(%s %s)' % (
                        tileBounds[0], tileBounds[1]), 4326, 21781
                    ).GetPoints()[0]
                    pointB = transformCoordinate('POINT(%s %s)' % (
                        tileBounds[2], tileBounds[3]), 4326, 21781
                    ).GetPoints()[0]
                    length = c2d.distance(pointA, pointB)
                    if tiles.fullonly == 1:
                        msg += 'WARNING: stats are approximative because ' \
                               'fullonly is activated!\n'
                    msg += 'At zoom %s:\n' % zoom
                    msg += 'We expect %s tiles overall\n' % nbTiles
                    msg += 'Min X is %s, Max X is %s\n' % (tileMinX, tileMaxX)
                    msg += '%s columns over X\n' % xCount
                    msg += 'Min Y is %s, Max Y is %s\n' % (tileMinY, tileMaxY)
                    msg += '%s rows over Y\n' % yCount
                    msg += '\n'
                    msg += 'A tile side is around %s meters' % int(round(length))
                    if nbTiles > 0 and nbObjects is not None:
                        msg += 'We have an average of about %s triangles ' \
                               'per tile\n' % int(round(nbObjects / nbTiles))
                    msg += '\n\n'
            msg += '%s tiles in total.' % total
        except Exception as e:
            logger.error('An error occured during statistics collection')
            logger.error('%s' % e, exc_info=True)
            raise Exception(e)
        finally:
            db.userEngine.dispose()

        return (total, msg)

    def numOfTiles(self):
        (total, msg) = self._stats(False)
        return total

    def stats(self):
        (total, msg) = self._stats(True)
        logger.info(msg)

    def statsNoDb(self):
        (total, msg) = self._stats(False)
        logger.info(msg)
