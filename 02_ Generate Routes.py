# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/routing.git. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/scalable-route-generation.

# COMMAND ----------

# MAGIC %md The purpose of this notebook is to demonstrate how the the OSRM software running within a Databricks cluster can be used to generate routing data.  

# COMMAND ----------

# MAGIC %md ## Introduction
# MAGIC 
# MAGIC With the software and map file assets in place, we have launched a cluster (with multiple worker nodes) that has deployed an instance of the OSRM Backend Server on each worker.
# MAGIC </p>
# MAGIC 
# MAGIC <img src='https://brysmiwasb.blob.core.windows.net/demos/images/osrm_scaled_deployment2.png' width=500>
# MAGIC </p>
# MAGIC Using point data within a Spark dataframe which by default is distributed across these worker nodes, we can define a series of functions to make local calls to these server instances in order to generate routing data in a scalable manner.   

# COMMAND ----------

# DBTITLE 1,Install Required Libraries
# MAGIC %pip install tabulate databricks-mosaic

# COMMAND ----------

# DBTITLE 1,Import Required Libraries
import requests

import pandas as pd
import numpy as np
import json

import itertools

import subprocess

import pyspark.sql.functions as fn
from pyspark.sql.types import *

# mosaic import and configuration
import mosaic as mos
spark.conf.set('spark.databricks.labs.mosaic.geometry.api', 'ESRI')
spark.conf.set('spark.databricks.labs.mosaic.index.system', 'H3')
mos.enable_mosaic(spark, dbutils)

from tabulate import tabulate

# COMMAND ----------

# MAGIC %md ## Step 1: Verify Server Running on Each Worker
# MAGIC 
# MAGIC Our first step is to verify the OSRM Backend Server is running on each worker node as expected.  To do this, we need to uncover the IP address used by each worker node in our cluster which we will do using an old-school Spark RDD which will force a small dataset to be distributed across the workers in our cluster.
# MAGIC 
# MAGIC To better understand this, it's helpful to know that the memory and processor resources available on each worker node are divided between Java Virtual Machines (JVMs).  These JVMs are referred to as *Executors* and are responsible for housing a subset of the data in a Spark RDD or Spark dataframe.  There is typically a one-to-one relationship between an Executor and a worker node but this is not always the case.
# MAGIC 
# MAGIC The [*sc.defaultParallelism*](https://spark.apache.org/docs/latest/api/python/reference/api/pyspark.SparkContext.defaultParallelism.html#pyspark.SparkContext.defaultParallelism) property keeps track of the number of processors availalbe across the worker nodes in a cluster, and by defining a Spark RDD using a parallelized range of values equivalent to this number, we are associating one integer value with each virtual core. The [*sc.runJob*](https://spark.apache.org/docs/latest/api/python/reference/api/pyspark.SparkContext.runJob.html) method then forces the the Python [*subprocess.run*](https://docs.python.org/3/library/subprocess.html#subprocess.run) method to run a local instance of the *hostname -I* command which retrieves the public IP address of the machine on which each of the value in the RDD. The output is returned as a list which is then transformed into a Python set to return just the unique IP values identified by the command.
# MAGIC 
# MAGIC While that sounds like a lot of explanation for such a simple task, please note that this same pattern will be coming into play with a different type of function call later in this notebook:

# COMMAND ----------

# DBTITLE 1,Get Worker Node IP Addresses
# generate RDD to span each executor on each worker
myRDD = sc.parallelize(range(sc.defaultParallelism))

# get set of ip addresses
ip_addresses = set( # conversion to set deduplicates output
  sc.runJob(
    myRDD, 
    lambda _: [subprocess.run(['hostname','-I'], capture_output=True).stdout.decode('utf-8').strip()] # run hostname -I on each executor
    )
  )

ip_addresses

# COMMAND ----------

# MAGIC %md Now that we know the IP addresses of our worker nodes, we can quickly test the response of each OSRM Backend Server listening on default port 5000 by requesting a routing response from each:

# COMMAND ----------

# DBTITLE 1,Test Each Worker for a Routing Response
responses = []

# for each worker ip address
for ip in ip_addresses:
  
  # get a response from the osrm backend server
  resp = requests.get(f'http://{ip}:5000/route/v1/driving/-74.005310,40.708750;-73.978691,40.744850').text
  responses += [(ip, resp)]
  
# display responses generated by each worker
display(
  pd.DataFrame(responses, columns=['ip','response'])
  )

# COMMAND ----------

# MAGIC %md ## Step 2: Retrieve Data for Route Generation
# MAGIC 
# MAGIC In order to demonstrate how the routing capabilities in our cluster can be used, we'll need to acquire some data from which we might generate routes.  The [*NYC Taxi* dataset](https://www1.nyc.gov/site/tlc/about/tlc-trip-record-data.page) available by default within each Databricks workspace provides easy access to such data:

# COMMAND ----------

# DBTITLE 1,Display Details about NYC Taxi Dataset
displayHTML('https://www1.nyc.gov/site/tlc/about/tlc-trip-record-data.page')

# COMMAND ----------

# MAGIC %md The NYC Taxi (*yellow cab*) dataset currently consists of over 160-million records.  To keep our work manageable, we'll focus on taxi rides within a narrowly defined range of time:

# COMMAND ----------

# DBTITLE 1,Access the NYC Taxi Data
nyc_taxi = (
  spark.read
  .format('delta')
  .load('dbfs:/databricks-datasets/nyctaxi/tables/nyctaxi_yellow/')
  .filter(fn.expr("pickup_datetime < '2016-01-01 00:00:00' AND dropoff_datetime > '2016-01-01 00:00:00'")) # stuck in cab at midnight on new years day
  .filter(fn.expr('pickup_latitude is not null and dropoff_latitude is not null')) # valid coordinates
  .withColumn('trip_meters', fn.expr('trip_distance * 1609.34'))
  .withColumn('trip_seconds', fn.expr('datediff(second, pickup_datetime, dropoff_datetime)'))
  )

display(nyc_taxi.limit(10))

# COMMAND ----------

# DBTITLE 1,Row Count for Dataset
nyc_taxi.count()

# COMMAND ----------

# MAGIC %md Per the data dictionary information supplied by the data provider, the fields in this dataset represent:
# MAGIC </p>
# MAGIC 
# MAGIC * **vendor_id** - A code indicating the TPEP provider that provided the record:
# MAGIC <br>
# MAGIC 1= Creative Mobile Technologies, LLC.<br>
# MAGIC 2= VeriFone Inc.<br>
# MAGIC * **pickup_datetime** - The date and time when the meter was engaged.
# MAGIC * **dropoff_datetime** - The date and time when the meter was disengaged.
# MAGIC * **passenger_count** The number of passengers in the vehicle. This is a driver-entered value.
# MAGIC * **trip_distance** - The elapsed trip distance in miles reported by the taximeter.
# MAGIC * **pickup_longitude, pickup_latitude** - TLC Taxi Zone in which the taximeter was engaged.
# MAGIC * **rate_code_id** - The final rate code in effect at the end of the trip:<br>
# MAGIC 1 = Standard rate<br>
# MAGIC 2 = JFK<br>
# MAGIC 3 = Newark<br>
# MAGIC 4 = Nassau or Westchester<br>
# MAGIC 5 = Negotiated fare<br>
# MAGIC 6 = Group ride<br>
# MAGIC * **store_and_fwd_flag** - This flag indicates whether the trip record was held in vehicle
# MAGIC memory before sending to the vendor, aka “store and forward,”
# MAGIC because the vehicle did not have a connection to the server:<br>
# MAGIC Y= store and forward trip<br>
# MAGIC N= not a store and forward trip.
# MAGIC * **dropoff_longitude, dropoff_latitude** - TLC Taxi Zone in which the taximeter was disengaged.
# MAGIC * **payment_type** A numeric code signifying how the passenger paid for the trip:<br>
# MAGIC 1= Credit card<br>
# MAGIC 2= Cash<br>
# MAGIC 3= No charge<br>
# MAGIC 4= Dispute<br>
# MAGIC 5= Unknown<br>
# MAGIC 6= Voided trip
# MAGIC * **fare_amount** - The time-and-distance fare calculated by the meter.
# MAGIC * **extra** - Extras and surcharges. Currently, this only includes the $0.50 and $1 rush hour and overnight charges.
# MAGIC * **mta_tax** - $0.50 MTA tax that is automatically triggered based on the metered rate in use.
# MAGIC * **tip_amount** – This field is automatically populated for credit card tips. Cash tips are not included.
# MAGIC * **tolls_amount** - Total amount of all tolls paid in trip.
# MAGIC * **total_amount** - The total amount charged to passengers. Does not include cash tips.
# MAGIC 
# MAGIC In addition to these fields, we've calculated two fields, **trip_meters** and **trip_seconds**, to provide distance and duration information in a manner consistent with what we will receive from the OSRM Backend Server.

# COMMAND ----------

# MAGIC %md ## Step 3: Get Trip Routes
# MAGIC 
# MAGIC The NYC Taxi dataset records the starting and ending point for each trip.  While we don't know the exact route, we can use the OSRM Backend Server [*route* method](http://project-osrm.org/docs/v5.5.1/api/#route-service) to identify a likely best route if that ride were to be taken today. To enable this, we'll write a function that will allow us to pass in the longitudes and latitudes for our pick-up and drop-off points for each trip.  This function will use that data to request a route from the OSRM Backend Server and return the resulting JSON document:

# COMMAND ----------

# DBTITLE 1,Define Function to Get Route
@fn.pandas_udf(StringType())
def get_osrm_route(
  start_longitudes: pd.Series, 
  start_latitudes:pd.Series, 
  end_longitudes: pd.Series, 
  end_latitudes: pd.Series
  ) -> pd.Series:
   
  # combine inputs to form dataframe
  df = pd.concat([start_longitudes, start_latitudes, end_longitudes, end_latitudes], axis=1)
  df.columns = ['start_lon','start_lat','end_lon','end_lat']

  # internal function to get route for a given row
  def _route(row):
    r = requests.get(
      f'http://127.0.0.1:5000/route/v1/driving/{row.start_lon},{row.start_lat};{row.end_lon},{row.end_lat}?alternatives=true&steps=false&geometries=geojson&overview=simplified&annotations=false'
    )
    return r.text
  
  # apply routing function row by row
  return df.apply(_route, axis=1)

# COMMAND ----------

# MAGIC %md To understand this function, remember that the data in our dataframe has been divided into subsets (partitions) that are distributed across the Executors aligned with the virtual cores on the worker nodes on our cluster.  (Please see the explanation in the section above on retrieving IP addresses if you need an explanation of the concept of an Executor.)  When we apply this function to our Spark dataframe, it will be applied to each partition in a parallel manner depending on the parallelism of the dataframe itself.
# MAGIC 
# MAGIC Through the arguments specified for this function, values from each partition will be received.  Each argument is mapped to a column and multiple rows worth of values from each column are received as a pandas Series with each argument. The number of values received with the series depends on the partition size as well as the *spark.databricks.execution.pandasUDF.maxBatchesToPrefetch* configuration setting.
# MAGIC 
# MAGIC The values in each series are sorted in the same order.  If we concatenate these series, we can recreate the rows of data found within the partition. To each row of the resulting pandas Dataframe, we apply an internally defined function makes a request to the local instance of the OSRM Backend Server.  The backend server returns routing information as a JSON string. That JSON string is returned for each row in the pandas Dataframe and the resulting series of returned values is sent from the outer function back to the Spark engine to be incorporated into the Spark dataframe.
# MAGIC 
# MAGIC This overall pattern of receiving a set of values as pandas Series for each argument defined in the user-defined function (UDF) and returning a corresponding set of results as a pandas Series is what makes this function a pandas Series-to-Series user-defined function.  You can read more about this type of pandas UDF [here](https://docs.databricks.com/spark/latest/spark-sql/udf-python-pandas.html).
# MAGIC 
# MAGIC To apply our pandas UDF to our data, we can simply use it in the context of a [*withColumn*](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.withColumn.html) method call as follows:

# COMMAND ----------

# DBTITLE 1,Retrieve Routes
display(
  nyc_taxi
    .withColumn(
      'osrm_route',
      get_osrm_route('pickup_longitude','pickup_latitude','dropoff_longitude','dropoff_latitude')
      )
    .selectExpr(
      'pickup_datetime',
      'dropoff_datetime',
      'pickup_longitude',
      'pickup_latitude',
      'dropoff_longitude',
      'dropoff_latitude',
      'osrm_route',
      'fare_amount',
      'trip_meters',
      'trip_seconds'
      )
    .limit(10)
  )

# COMMAND ----------

# MAGIC %md The results of the function call is a JSON string.  We've returned it as a string as opposed to a complex data type simply because the pandas UDF does not have the ability to marshal all complex types between a pandas UDF and the Spark engine. So if we need to convert the string into a complex data representation, we need to do this once the function has completed its work. 
# MAGIC 
# MAGIC For example, here we've provided a string-based representation of the JSON schema and have applied the [*from_json*](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.from_json.html#pyspark.sql.functions.from_json) function with this schema as an argument to convert the returned value into the desired complex type:
# MAGIC 
# MAGIC **NOTE** If you prefer to define a schema using the traditional PySpark data type representations, as will be shown later in this notebook, that works fine as well.

# COMMAND ----------

# DBTITLE 1,Convert Route JSON to Complex Data Type Representation
# schema for the json document
response_schema = '''
  STRUCT<
    code: STRING, 
    routes: 
      ARRAY<
        STRUCT<
          distance: DOUBLE, 
          duration: DOUBLE, 
          geometry: STRUCT<
            coordinates: ARRAY<ARRAY<DOUBLE>>, 
            type: STRING
            >, 
          legs: ARRAY<
            STRUCT<
              distance: DOUBLE, 
              duration: DOUBLE, 
              steps: ARRAY<STRING>, 
              summary: STRING, 
              weight: DOUBLE
              >
            >, 
          weight: DOUBLE, 
          weight_name: STRING
          >
        >,
      waypoints: ARRAY<
        STRUCT<
          distance: DOUBLE, 
          hint: STRING, 
          location: ARRAY<DOUBLE>, 
          name: STRING
          >
        >
      >
  '''

# retrieve routes and convert json to struct
nyc_taxi_routes = (
  nyc_taxi
  .withColumn(
    'osrm_route',
    get_osrm_route('pickup_longitude','pickup_latitude','dropoff_longitude','dropoff_latitude')
    )
  .withColumn(
    'osrm_route',
    fn.from_json('osrm_route',response_schema)
    )
  .selectExpr(
    'pickup_datetime',
    'dropoff_datetime',
    'osrm_route',
    'trip_meters',
    'trip_seconds'
    )
  )


display(
  nyc_taxi_routes.limit(10)
  )

# COMMAND ----------

# MAGIC %md The structure of the JSON document is defined by the OSRM Backend Server.  These elements can be extracted using simple dot-notation references:
# MAGIC 
# MAGIC **NOTE** Within the JSON document, routes is presented as an array even though there should be only one route per document.  The [*explode*](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.explode.html#pyspark.sql.functions.explode) function expands the array, duplicating the other fields around each element in the array.  With only one route in the routes array, this function call should not expand the size of the dataset.

# COMMAND ----------

# DBTITLE 1,Retrieve Distance and Duration from Routes
display(
   nyc_taxi_routes
    .withColumn('route', fn.explode('osrm_route.routes'))
    .withColumn('route_meters', fn.col('route.distance'))
    .withColumn('route_seconds', fn.col('route.duration'))
    .selectExpr(
      'pickup_datetime',
      'dropoff_datetime',
      'trip_meters',
      'route_meters',
      'trip_seconds',
      'route_seconds'
      )
    .limit(10)
  )

# COMMAND ----------

# MAGIC %md In addition to attributes such as distance and duration, the route information returned by the OSRM Backend Server contains a *geometry* element which adheres to the [GeoJSON format](https://datatracker.ietf.org/doc/html/rfc7946). Using the [Databricks Mosaic](https://databricks.com/blog/2022/05/02/high-scale-geospatial-processing-with-mosaic.html) library's [*st_geomfromgeojson*](https://databrickslabs.github.io/mosaic/api/geometry-constructors.html#st-geomfromgeojson) and [*st_aswkb*](https://databrickslabs.github.io/mosaic/api/geometry-accessors.html#st-aswkb) methods, we can convert this element into a standard representation:

# COMMAND ----------

# DBTITLE 1,Get Route Geometry
nyc_taxi_geometry = (
  nyc_taxi_routes
    .withColumn('route', fn.explode('osrm_route.routes')) # explode routes array
    .withColumn('geojson', fn.to_json(fn.col('route.geometry')))
    .withColumn('geom', mos.st_aswkb(mos.st_geomfromgeojson('geojson')))
    .drop('osrm_route')
  )

display(nyc_taxi_geometry.limit(10))

# COMMAND ----------

# MAGIC %md This standard geometry can then be visualized using a [Kepler visualization](https://databrickslabs.github.io/mosaic/usage/kepler.html) to help us verify the information in our route data:
# MAGIC 
# MAGIC **NOTE** The syntax of the [*mosaic_kepler magic* command](https://databrickslabs.github.io/mosaic/usage/kepler.html) is *dataset* *column_name* *feature_type* \[*row_limit*\].  Using the toggle in the top left-hand corner of the display, you can adjust various aspects of the visualization.

# COMMAND ----------

# DBTITLE 1,Visualize Routes
# MAGIC %%mosaic_kepler
# MAGIC 
# MAGIC nyc_taxi_geometry geom  geometry 500

# COMMAND ----------

# MAGIC %md But of course, we are not limited to retrieving routing data from the OSRM Backend Server.  If our goal were to optimize the movement between points, we may need to construct a traversal times table.  To do this, we can write a function to call the OSRM Backend Server's [*table* method](http://project-osrm.org/docs/v5.5.1/api/#table-service):

# COMMAND ----------

# DBTITLE 1,Get Driving Times Table
@fn.pandas_udf(StringType())
def get_driving_table(
  points_arrays: pd.Series
  ) -> pd.Series:

  # internal function to get table for points in an array
  def _table(points_array):
    
    points = ';'.join(points_array)
    
    r = requests.get(
      f'http://127.0.0.1:5000/table/v1/driving/{points}'
    )
    
    return r.text
  
  # apply table function row by row
  return points_arrays.apply(_table)

# COMMAND ----------

# MAGIC %md To call this function, we need to provide a collection of points.  The NYC Taxi data doesn't really provide a good means for this, let's arbitrarily that we needed to tackle all pickup points within 1 second intervals:
# MAGIC 
# MAGIC **NOTE** The aggregation in this example is contrived simply to demonstrate how values are passed to the *get_driving_table* function defined above.

# COMMAND ----------

# DBTITLE 1,Retrieve Driving Times Tables
# schema for driving table
response_schema = StructType([
  StructField('code',StringType()),
  StructField('destinations',ArrayType(
    StructType([
      StructField('hint',StringType()),
      StructField('distance',FloatType()),
      StructField('name',StringType()),
      StructField('location',ArrayType(FloatType()))
      ])
     )
   ),
  StructField('durations',ArrayType(ArrayType(FloatType()))),
  StructField('sources',ArrayType(
    StructType([
      StructField('hint',StringType()),
      StructField('distance',FloatType()),
      StructField('name',StringType()),
      StructField('location',ArrayType(FloatType()))
      ])
    ))
  ])

# retrieve driving table and extract matrix
driving_tables = (
  nyc_taxi
  .withColumn('pickup_point', fn.expr("concat(pickup_longitude,',',pickup_latitude)"))
  .withColumn('pickup_window', fn.expr("window(pickup_datetime, '1 SECONDS')"))
  .groupBy('pickup_window')
    .agg(fn.collect_set('pickup_point').alias('pickup_points'))
  .filter(fn.expr('size(pickup_points) > 1')) # more than one point required for table
  .withColumn('driving_table', get_driving_table('pickup_points'))
  .withColumn('driving_table', fn.from_json('driving_table', response_schema))
  .withColumn('driving_table_durations', fn.col('driving_table.durations'))
  )  

display(driving_tables.limit(10))

# COMMAND ----------

# MAGIC %md Examining the matrix extracted from the driving table, it's important to note that estimated driving times (in seconds) are not symmetric as travel in different directions may be subject to differences in routing.  Here we show this for one of the matrices taken from the dataset:

# COMMAND ----------

# DBTITLE 1,Display a Single Driving Table
# generate a single table
driving_table = driving_tables.limit(1).collect()[0]['driving_table_durations']

# print driving table
print(
    tabulate(
      np.array(driving_table),
      tablefmt='grid'
      )
    )

# COMMAND ----------

# MAGIC %md The *route* and *table* methods of the OSRM Backend Server are two of several methods available through the server's REST API.  The full list of methods include:
# MAGIC </p>
# MAGIC 
# MAGIC * [route](http://project-osrm.org/docs/v5.5.1/api/#route-service) - finds the fastest route between coordinates in the supplied order
# MAGIC * [nearest](http://project-osrm.org/docs/v5.5.1/api/#nearest-service) - snaps a coordinate to the street network and returns the nearest n matches
# MAGIC * [table](http://project-osrm.org/docs/v5.5.1/api/#table-service) - computes the duration of the fastest route between all pairs of supplied coordinates
# MAGIC * [match](http://project-osrm.org/docs/v5.5.1/api/#match-service) - snaps given GPS points to the road network in the most plausible way
# MAGIC * [trip](http://project-osrm.org/docs/v5.5.1/api/#trip-service) - solves the Traveling Salesman Problem using a greedy heuristic (farthest-insertion algorithm)
# MAGIC * [tile](http://project-osrm.org/docs/v5.5.1/api/#tile-service) - generates Mapbox Vector Tiles that can be viewed with a vector-tile capable slippy-map viewer
# MAGIC 
# MAGIC To make any of these accessible during Spark dataframe processing, simply construct a pandas UDF around the HTTP REST API call as demonstrated above, return the resulting JSON as a string, and apply the appropriate schema to the result as demonstrted in the previous examples.

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC &copy; 2022 Databricks, Inc. All rights reserved. The source in this notebook is provided subject to the [Databricks License](https://databricks.com/db-license-source).  All included or referenced third party libraries are subject to the licenses set forth below.
# MAGIC 
# MAGIC | library                                | description             | license    | source                                              |
# MAGIC |----------------------------------------|-------------------------|------------|-----------------------------------------------------|
# MAGIC | OSRM Backend Server                                  | High performance routing engine written in C++14 designed to run on OpenStreetMap data | BSD 2-Clause "Simplified" License    | https://github.com/Project-OSRM/osrm-backend                   |
# MAGIC | Mosaic | An extension to the Apache Spark framework that allows easy and fast processing of very large geospatial datasets | Databricks License| https://github.com/databrickslabs/mosaic | 
# MAGIC | Tabulate | pretty-print tabular data in Python | MIT License | https://pypi.org/project/tabulate/ |
