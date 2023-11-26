from histpy import Histogram
from cosipy import SpacecraftFile
import healpy as hp
from mhealpy import HealpixMap
import numpy as np
import os
import multiprocessing
from itertools import product
from .fast_norm_fit import FastNormFit as fnf
from pathlib import Path
from cosipy.response import FullDetectorResponse
import time

class FastTSMap():
    
    def __init__(self, data, bkg_model, orientation, response_path, cds_frame = "local", scheme = "RING"):
        
        """
        Initialize the instance
        
        Parameters
        ----------
        data: histpy.Histogram; observed data, whichincludes counts from both signal and background
        bkg_model: histpy.Histogram; background model, which includes the background counts to model the background in observed data
        orientation: cosipy.SpacecraftFile; the orientation of the spacecraft when data are collected
        response_path: pathlib.Path; the path to the response file
        cds_frame: str; "local" or "galactic", it's the frame of the data, bkg_model and the response
        
        Returns
        -------
        """
        
        self._data = data.project(["Em", "PsiChi", "Phi"])
        self._bkg_model = bkg_model.project(["Em", "PsiChi", "Phi"])
        if not isinstance(orientation, SpacecraftFile):
            raise TypeError("The orientation must be a cosipy.SpacecraftFile object!")
        self._orientation = orientation
        self._response_path = Path(response_path)
        self._cds_frame = cds_frame
        self._scheme = scheme
        
    @staticmethod
    def slice_energy_channel(hist, channel_start, channel_stop):
        """
        Slice one or more bins along first axis
        
        Parameters
        ----------
        hist: histpy.Histogram; the hist to be sliced
        channel_start: int; the start of the slice (inclusive)
        channel_stop: int; the stop of the slice (exclusive)
        
        Returns
        -------
        sliced_hist: histpy.Histogram: the sliced hist
        """
        
        sliced_hist = hist.slice[channel_start:channel_stop,:]
        
        return sliced_hist
    
    @staticmethod
    def get_hypothesis_coords(nside, scheme = "RING", coordsys = "galactic"):
        
        """
        Get a list of hypothesis coordinates
        
        Parameters
        ----------
        nside: int; the nside of the map
        
        Returns
        -------
        hypothesis_coords: list; the list of the hypothesis coordinates at the center of each pixel
        """
        
        data_array = np.zeros(hp.nside2npix(nside))
        ts_temp = HealpixMap(data = data_array, scheme = scheme, coordsys = coordsys)
        
        hypothesis_coords = []
        for i in np.arange(data_array.shape[0]):
            hypothesis_coords += [ts_temp.pix2skycoord(i)]
            
        return hypothesis_coords
    
    
    @staticmethod
    def get_cds_array(hist, energy_channel):
        
        """
        Get the flattened cds array from input Histogram.
        
        Parameters
        -----------
        hist: histpy.Histogram; input Histogram
        energy_channel: list; [lower_channel, upper_chanel]
        
        Returns
        -------
        cds_array
        
        """
        if not isinstance(hist, Histogram):
            raise TypeError("Please input hist must be a histpy.Histogram object.")
        
        hist_axes_labels = hist.axes.labels
        cds_labels = ["PsiChi", "Phi"]
        if not all([label in hist_axes_labels for label in cds_labels]):
            raise ValueError("The data doesn't contain the full Compton Data Space!")
            
        hist = hist.project(["Em", "PsiChi", "Phi"]) # make sure the first axis is the measured energy
        hist_cds_sliced = FastTSMap.slice_energy_channel(hist, energy_channel[0], energy_channel[1])   
        hist_cds = hist_cds_sliced.project(["PsiChi", "Phi"])
        cds_array = np.array(hist_cds.to_dense()[:]).flatten()  # here [:] is equivalent to [:, :]
        
        return cds_array
        
        
    @staticmethod
    def get_ei_cds_array(hypothesis_coord, energy_channel, orientation, response_path, spectrum, cds_frame):
                         
        """
        Get the expected counts in CDS in local or galactic frame.
        
        Parameters
        ----------
        hypothesis_coord
        energy_channel
        orientation
        response_path
        spectrum
        
        Returns
        -------
        cds_array
        """
                         
        # check inputs, will complete later
                         
        # the local and galactic frame works very differently, so we need to compuate the point source response (psr) accordingly 
        if cds_frame == "local":
            
            # convert the hypothesis coord to the local frame (Spacecraft frame)
            hypothesis_in_sc_frame = orientation.get_target_in_sc_frame(target_name = "Hypothesis", 
                                                                        target_coord = hypothesis_coord, 
                                                                        quiet = True)
            # get the dwell time map: the map of the time spent on each pixel in the local frame
            dwell_time_map = orientation.get_dwell_map(response = response_path)
            
            # convolve the response with the dwell_time_map to get the point source response
            with FullDetectorResponse.open(response_path) as response:
                psr = response.get_point_source_response(dwell_time_map)

        elif cds_frame == "galactic":
            
            with FullDetectorResponse.open(response_path) as response:
            
                # get scatt_map, currently I have a shallow understanding of scatt_map
                # play more with it when possible to deep the understanding
                scatt_map = orientation.get_scatt_map(nside = response.nside * 2, coordsys = 'galactic')
            
                # convolve the response with the scatt_map to get the point source response in the galactic frame
                psr = response.get_point_source_response(coord = hypothesis_coord, scatt_map = scatt_map)
                
        else:
            raise ValueError("The point source response must be calculated in the local and galactic frame. Others are not supported (yet)!")
            
        # convolve the point source reponse with the spectrum to get the expected counts
        expectation = psr.get_expectation(spectrum)

        # slice energy channals and project it to CDS
        ei_cds_array = FastTSMap.get_cds_array(expectation, energy_channel)
        
        return ei_cds_array
    
    @staticmethod
    def fast_ts_fit(hypothesis_coord, 
                    energy_channel, data_cds_array, bkg_model_cds_array, 
                    orientation, response_path, spectrum, cds_frame,
                    ts_nside, ts_scheme):
        
        # get the pix number of the ts map
        data_array = np.zeros(hp.nside2npix(ts_nside))
        ts_temp = HealpixMap(data = data_array, scheme = ts_scheme, coordsys = "galactic")
        pix = ts_temp.ang2pix(hypothesis_coord)
        
        # get the expected counts in the flattened cds array
        ei_cds_array = FastTSMap.get_ei_cds_array(hypothesis_coord = hypothesis_coord, cds_frame = cds_frame,
                                                  energy_channel = energy_channel, orientation = orientation, 
                                                  response_path = response_path, spectrum = spectrum)
        
        # start the fit
        fit = fnf(max_iter=1000)
        result = fit.solve(data_cds_array, bkg_model_cds_array, ei_cds_array)
        
        return [pix, result[0], result[1], result[2], result[3]]

        
    def parallel_ts_fit(self, hypothesis_coords, energy_channel, spectrum, ts_scheme = "RING"):
        
        """
        Perform parallel computation on all the hypothesis coordinates.
        
        Parameters
        ----------
        hypothesis_coords: list; a list of the hypothesis coordinates
        energy_channel: list; the energy channel you want to use: [lower_channel, upper_channel]
        spectrum: astromodels; the model to be placed at the hypothesis coordinates
        ts_scheme: str; "RING" or "NESTED"
        
        Returns
        -------
        ts_values
        """
        
        # decide the ts_nside from the list of hypothesis coordinates
        ts_nside = hp.npix2nside(len(hypothesis_coords))
        
        # get the data_cds_array
        data_cds_array = FastTSMap.get_cds_array(self._data, energy_channel)
        bkg_model_cds_array = FastTSMap.get_cds_array(self._bkg_model, energy_channel)
        
        if (data_cds_array.flatten()[bkg_model_cds_array.flatten()==0]!=0).sum() != 0:
            raise ValueError("You have data!=0 but bkg=0, check your inputs!")
        
        start = time.time()
        
        cores = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(processes=cores)
        results = pool.starmap(FastTSMap.fast_ts_fit, product(hypothesis_coords, [energy_channel], [data_cds_array], [bkg_model_cds_array], 
                                                             [self._orientation], [self._response_path], [spectrum], [self._cds_frame], 
                                                             [ts_nside], [ts_scheme]))
            
        pool.close()
        pool.join()
        
        end = time.time()
        
        elapsed_seconds = end - start
        elapsed_minutes = elapsed_seconds/60
        print(f"The time used for the parallel TS map computation is {elapsed_minutes} minutes")
        
        
        return results
        
    