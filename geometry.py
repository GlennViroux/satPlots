'''
Positions manager class
'''
import os
import pytz
import math
import pandas as pd
import numpy as np
import datetime
import configparser
from pathlib import Path
from skyfield.api import EarthSatellite,load
#import cartopy.crs as ccrs
#import matplotlib.pyplot as plt
#from matplotlib.cm import get_cmap

#from plotting import Plotting
from data_download import Celestrak,IGS
from basics import SpaceVector
from satplots_logging import get_logger
from grid import Grid
from projections import ecef2latlonheight,latlonheight2ecef
from conversions import norad2prn
from snippets import df2geojsonLineString,df2geojsonSatPoints,df2geojsonStationPoints,check_output,write_to_file


EARTH_FLATTE_GRS80 = 1.0/298.257222101

class Geometry:
    def __init__(self,config_file="./config/config.ini"):
        self.grid_points = 400

        conf_file = Path(config_file)
        if not conf_file.exists():
            raise Exception(f"Configuration file {config_file} does not exist, exiting.")
        self.config = configparser.ConfigParser()
        self.config.read(config_file)

        self.use_cpp = self.config.getboolean('general','use_cpp')

        log_file = "./logs/geometry_log.txt"
        self.logger = get_logger(log_file)
        
        self.ts = load.timescale()
        self.tles_df = pd.DataFrame.from_dict({"norad_id":[],"epoch":[],"line1":[],"line2":[]})
        self.igs_stations_df = pd.DataFrame.from_dict({"Station":[],"StationFull":[],"X":[],"Y":[],"Z":[],"ReceiverName":[],"AntennaName":[],"ClockType":[]})

    def load_tles_celestrak(self,start,end):
        self.logger.info("Loading TLEs from Celestrak")
        if not isinstance(start,datetime.date):
            raise Exception("Function load_tles_celestrak: No date object provided")
        if not isinstance(end,datetime.date):
            raise Exception("Function load_tles_celestrak: No date object provided")

        tle_start = start - datetime.timedelta(days=14)
        tle_end = end + datetime.timedelta(days=14)
        dates = pd.date_range(tle_start,tle_end)

        tles_found = False
        for date in dates:
            tles = Celestrak.get_tles(date)
            if not tles:
                continue
            tles_found = True
            df_dict = {"norad_id":[],"epoch":[],"line1":[],"line2":[]}
            for tle in tles:
                df_dict["norad_id"].append(tle.norad_id)
                df_dict["epoch"].append(tle.epoch)
                df_dict["line1"].append(tle.line1)
                df_dict["line2"].append(tle.line2)
            new_df = pd.DataFrame.from_dict(df_dict)
            self.tles_df = pd.concat([self.tles_df,new_df])
        
        if not tles_found:
            self.logger.error(f"No TLEs were found for start {start} and end {end}.")

    def load_IGS_stations(self):
        self.logger.info("Loading IGS stations")
        self.igs_stations_df = IGS.get_IGS_stations_df_full()

        stations = [stat.upper() for stat in self.config["general"]["stations"].split(",")]
        if len(stations)==1 and not stations[0]:
            stations = IGS.get_IGS_station_list()

        self.igs_stations_df = self.igs_stations_df[self.igs_stations_df.Station.isin(stations)]

    def get_closest_tle(self,norad_id,epoch):
        '''
        Return the closest (epoch-wise) tle that is loaded.
        '''
        self.logger.debug(f"Getting closest TLE for {norad_id} and {epoch}")
        if isinstance(epoch,str):
            epoch = datetime.datetime.strptime(epoch,"%Y/%m/%d-%H:%M:%S")
        if not isinstance(epoch,datetime.datetime):
            raise Exception("Bad epoch provided, fix it")

        df = self.tles_df[self.tles_df.norad_id==norad_id]
        if df.empty:
            raise Exception(f"No valid TLE found for {norad_id} and {epoch}")

        epoch_diff = (df.epoch - epoch).apply(lambda x: abs(x.total_seconds())).rename("epoch_diff")
        df = pd.concat([df,epoch_diff],axis=1)
        df.sort_values("epoch_diff",inplace=True)
        return df.iloc[0]
      
    def get_sat_pos(self,satellite,epoch):
        self.logger.debug(f"Getting position {epoch}")
        if isinstance(epoch,str):
            epoch = datetime.datetime.strptime(epoch,"%Y/%m/%d-%H:%M:%S")

        timezone = pytz.timezone("UTC")
        epoch_aware = timezone.localize(epoch)
        t = self.ts.from_datetime(epoch_aware)
        geocentric = satellite.at(t)
        subpoint = geocentric.subpoint()

        return SpaceVector.from_llh(subpoint.latitude.degrees,subpoint.longitude.degrees,subpoint.elevation.m)

    def get_sat_positions(self,norad_id,start,end,sampling=5):
        self.logger.info(f"Getting all positions for {norad_id} between {start} and {end}")
        if isinstance(start,str):
            start = datetime.datetime.strptime(start,"%Y/%m/%d-%H:%M:%S")
        if isinstance(end,str):
            end = datetime.datetime.strptime(end,"%Y/%m/%d-%H:%M:%S")
        
        if not isinstance(start,datetime.datetime):
            raise Exception("Funcion get_sat_positions: No datetime object provided for start")
        if not isinstance(end,datetime.datetime):
            raise Exception("Funcion get_sat_positions: No datetime object provided for end")
        if not isinstance(sampling,int):
            raise Exception("Funcion get_sat_positions: No int object provided for sampling")

        number_of_epochs = (end-start)/datetime.timedelta(minutes=sampling)
        epochs = [(start + datetime.timedelta(minutes=sampling*i)) for i in range(int(number_of_epochs))]
        positions = []
        lats = []
        lons = []
        tle = self.get_closest_tle(norad_id,start)
        satellite = EarthSatellite(tle.line1, tle.line2, tle.norad_id, self.ts)
        for epoch in epochs:
            new_pos = self.get_sat_pos(satellite,epoch)
            positions.append(new_pos)
            lats.append(new_pos.lat)
            lons.append(new_pos.lon)
        df = pd.DataFrame(zip(epochs,positions,lats,lons),columns=["epoch","pos","lat","lon"])

        return df

    def get_station_pos(self,station):
        self.logger.debug(f"Getting position for station {station}")
        res = self.igs_stations_df[self.igs_stations_df.Station==station]
        if len(res)==0:
            raise Exception(f"No station data was found for station: {station}")

        result = SpaceVector(res.iloc[0].X,res.iloc[0].Y,res.iloc[0].Z,skip_llh=True)

        return result

    def get_elevation(self,station_pos,sat_pos):
        self.logger.debug(f"Getting elevation between station at {station_pos} and satellite at {sat_pos}")
        if not isinstance(station_pos,SpaceVector):
            raise Exception("Provided station position is not a SpaceVector.")
        if not isinstance(sat_pos,SpaceVector):
            raise Exception("Provided satellite position is not a SpaceVector.")

        station_to_sat = sat_pos - station_pos
        distance = station_to_sat.norm()
        station_normal = station_pos
        station_normal.z /= ((1.0 - EARTH_FLATTE_GRS80) * (1.0 - EARTH_FLATTE_GRS80))
        aux_norm = station_normal.norm()
        glenny = station_normal/aux_norm
        aux = station_to_sat.dot(glenny)

        if distance<1e-10:
            return 0

        sinE = aux/distance
        elev = math.degrees(math.asin(sinE))

        return elev 

    def get_stations_in_view(self,sat_pos):
        self.logger.debug(f"Getting stations in view for satellite at {sat_pos}")
        elev_mask = float(self.config["general"]["elevation_mask"])
        stations = self.igs_stations_df.Station

        stations_in_view = []
        for station in stations:
            stat_pos = self.get_station_pos(station)
            elevation = self.get_elevation(stat_pos,sat_pos)
            if elevation>=elev_mask:
                stations_in_view.append(station)

        return stations_in_view

    def get_stations_in_view_sat_track(self,norad_id,start,end):
        self.logger.info(f"Getting stations in view along the track for {norad_id} between {start} and {end}")
        sat_pos_df = self.get_sat_positions(norad_id,start,end)
        self.logger.info(f"Positions calculated!")
        stations_in_view = []
        number_stats_in_view = []
        norad_ids = []
        prns = []
        self.logger.info(f"Calculating stations in view for {norad_id}...")
        for _,row in sat_pos_df.iterrows():
            sat_pos = row.pos
            stats_in_view = self.get_stations_in_view(sat_pos)
            stations_in_view.append(stats_in_view)
            number_stats_in_view.append(len(stats_in_view))
            norad_ids.append(norad_id)
            prns.append(norad2prn(norad_id))

        new_df = pd.DataFrame(zip(number_stats_in_view,stations_in_view,norad_ids,prns),columns=["number_stations_in_view","stations_in_view","norad_id","prn"])

        df = pd.concat([sat_pos_df,new_df],axis=1)

        return df

    def calculate_all(self,start,end,norad_ids=None):
        self.logger.info(f"Calculating results for all norad ids between {start} and {end}")
        if isinstance(start,str):
            start = datetime.datetime.strptime(start,"%Y/%m/%d-%H:%M:%S")
        if norad_ids:
            norad_ids = norad_ids.split(",")
        else:
            norad_ids = Celestrak.get_norad_ids(start.date())

        basepath = Path("./output") / str(start.date().year) / str(start.date().month).zfill(2) / str(start.date().day).zfill(2)
        basepath.mkdir(parents=True,exist_ok=True)


        for norad_id in norad_ids:
            sat = norad2prn(norad_id)
            sat_points_check = check_output("sat_points",start.date(),sat)
            sat_track_check = check_output("sat_track",start.date(),sat)
            stations_check = check_output("stations",start.date())

            if sat_points_check and sat_track_check and stations_check:
                self.logger.info(f"Skipping norad id {norad_id} as results are already present.")
                continue
            elif not sat:
                self.logger.warning(f"Skipping norad id {norad_id}, norad2prn returned and error.")
                continue

            if self.use_cpp:
                self.logger.info(f"Calculating all elevations for {norad_id}")
                self.remove_cpp_tmp_files()
                sat_pos_df = self.get_sat_positions(norad_id,start,end)
                self.write_positions(start,end,norad_id,sat_pos_df)
                self.launch_cpp()
                cpp_df = pd.read_csv("./tmp/cpp_data_out.txt")
                df = self.get_cpp_df(norad_id,sat_pos_df,cpp_df)
                df2geojsonSatPoints(df,basepath / "sat_points")
                df2geojsonLineString(df,basepath / "sat_track")
                
            else:
                df = self.get_stations_in_view_sat_track(norad_id,start,end)
                df2geojsonSatPoints(df,basepath / "sat_points")
                df2geojsonLineString(df,basepath / "sat_track")

    def remove_cpp_tmp_files(self):
        tmp_file = Path("./tmp/cpp_data.txt")
        if tmp_file.exists():
            tmp_file.unlink()
        tmp_file_output = Path("./tmp/cpp_data_out.txt")
        if tmp_file_output.exists():
            tmp_file_output.unlink()

    def write_positions(self,start,end,norad_id,sat_pos_df):
        '''
        Write all station-sat positions in order to calculate the elevations using the C++ binary.
        '''
        epochs = sat_pos_df.epoch
        pos_sat = sat_pos_df.pos
        stations = self.igs_stations_df.Station
        stat_pos_x = self.igs_stations_df.X
        stat_pos_y = self.igs_stations_df.Y
        stat_pos_z = self.igs_stations_df.Z

        result = {'epoch':[],'station':[],'sat':[],'x_stat':[],'y_stat':[],'z_stat':[],'x_sat':[],'y_sat':[],'z_sat':[]}
        for i in range(len(epochs)):
            epoch = epochs[i]
            sat_pos = pos_sat[i]
            for j in range(len(stations)):
                station = stations[j]
                result['epoch'].append(epoch)
                result['station'].append(station)
                result['sat'].append(norad_id)
                result['x_stat'].append(stat_pos_x[j])
                result['y_stat'].append(stat_pos_y[j])
                result['z_stat'].append(stat_pos_z[j])
                result['x_sat'].append(sat_pos.x)
                result['y_sat'].append(sat_pos.y)
                result['z_sat'].append(sat_pos.z)
        
        df = pd.DataFrame.from_dict(result)

        write_to_file(df,"./tmp","cpp_data.txt")

    def launch_cpp(self):
        self.logger.info("Launching C++...")
        os.system("./cpp/main ./tmp/cpp_data.txt ./tmp/cpp_data_out.txt")
        self.logger.info("C++ done!")

    def get_cpp_df(self,norad_id,sat_pos_df:pd.DataFrame,cpp_df:pd.DataFrame):
        elev_mask = float(self.config["general"]["elevation_mask"])        
        epochs = sat_pos_df.epoch
        stations_in_view = []
        number_stations_in_view = []
        norad_ids = []
        prns = []
        for i in range(len(epochs)):
            epoch = str(epochs[i])
            cpp_filtered = cpp_df[(cpp_df.epoch==epoch) & (cpp_df.sat==norad_id) & (cpp_df.elev>=elev_mask)]
            stats = list(cpp_filtered.station)
            prn = norad2prn(norad_id)

            stations_in_view.append(stats)
            number_stations_in_view.append(len(stats))
            norad_ids.append(norad_id)
            prns.append(prn)

        new_df = pd.DataFrame(zip(number_stations_in_view,stations_in_view,norad_ids,prns),columns=["number_stations_in_view","stations_in_view","norad_id","prn"])
        df = pd.concat([sat_pos_df,new_df],axis=1)
        return df
    
    '''
    def plot_elevations(self,sat_pos):
        df_elev = self.calculate_elevations(sat_pos)

        fig,ax = Plotting.get_map()
        all_lats = list(pd.unique(df_elev.lat))
        all_lons = list(pd.unique(df_elev.lon))

        lats,lons = np.meshgrid(all_lons,all_lats)
        elevs = np.zeros_like(lats)
        for row in range(len(all_lats)):
            for col in range(len(all_lons)):
                elevs[row][col] = df_elev[(df_elev.lat==all_lats[row]) & (df_elev.lon==all_lons[col])].elev

        plt.contourf(lats,lons,elevs,10,cmap=get_cmap("viridis"),alpha=0.5,transform=ccrs.PlateCarree(),zorder=3)
        #sat_lat,sat_lon,sat_alt = ecef2latlonheight(sat_pos.x,sat_pos.y,sat_pos.z)
        ax.scatter(sat_pos.lon,sat_pos.lat,color='tab:red',label='sat position',transform=ccrs.PlateCarree(),zorder=3)

        #print("Glenny sat:",sat_lon,";",sat_lat,";",sat_alt)
        #print("Glenny sat alt:",sat_pos.lon,";",sat_pos.lat,";",sat_pos.height)

        stations_in_view = self.get_stations_in_view(sat_pos)
        for station in stations_in_view:
            station_pos = self.get_station_pos(station)
            #print("Glenny:",station_pos.lon,";",station_pos.lat,";",station_pos.height)
            ax.scatter(station_pos.lon,station_pos.lat,color='tab:green',label='station position',transform=ccrs.PlateCarree(),zorder=3)


        ax.set_title("Satellite position and elevation (degrees)",fontsize='xx-large')

        Plotting.unique_legend(ax)

        plt.colorbar(ax=ax,shrink=0.80)

        fig.savefig('glenny.png')
    '''

    def calculate_elevations(self,sat_pos):
        self.logger.info("Calculating elevations...")
        grid,_,_ = Grid.get_plane_grid(number_of_points=self.grid_points,height=6371*1000)
        elevs = []
        lats = []
        lons = []
        for pos_tuple in grid:
            lat,lon,alt = pos_tuple
            lats.append(lat)
            lons.append(lon)
            x,y,z = latlonheight2ecef(lat,lon,alt)
            pos_vect = SpaceVector(x,y,z)
            elev = self.get_elevation(pos_vect,sat_pos)
            elevs.append(elev)

        df_elev = pd.DataFrame(zip(lats,lons,elevs),columns=['lat','lon','elev'])

        return df_elev

        



