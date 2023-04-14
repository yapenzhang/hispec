import os
import sys
os.environ["OMP_NUM_THREADS"] = "1"
import json
import warnings
import numpy as np
from scipy.interpolate import interp1d, splrep, splev, RegularGridInterpolator#, CubicSpline
from scipy import signal
from scipy.ndimage import gaussian_filter
# from numpy.polynomial import polynomial as Poly
from numpy.polynomial import chebyshev as Chev
from astropy.io import fits
from PyAstronomy import pyasl
import pymultinest
import excalibuhr.utils as su 
from excalibuhr.data import SPEC2D
from petitRADTRANS import Radtrans
from petitRADTRANS import nat_cst as nc
from petitRADTRANS.retrieval import rebin_give_width as rgw
# import petitRADTRANS.poor_mans_nonequ_chem as pm
# from petitRADTRANS.retrieval import cloud_cond as fc
# from typeguard import typechecked
from sksparse.cholmod import cholesky
# import matplotlib.pyplot as plt 
# signal.savgol_filter
import pylab as plt
cmap = plt.get_cmap("tab10")

def getMM(species):
    """
    Get the molecular mass of a given species.

    This function uses the molmass package to
    calculate the mass number for the standard
    isotope of an input species. If all_iso
    is part of the input, it will return the
    mean molar mass.

    Args:
        species : string
            The chemical formula of the compound. ie C2H2 or H2O
    Returns:
        The molar mass of the compound in atomic mass units.
    """
    from molmass import Formula

    e_molar_mass = 5.4857990888e-4  # (g.mol-1) e- molar mass (source: NIST CODATA)

    if species == 'e-':
        return e_molar_mass
    elif species == 'H-':
        return Formula('H').mass + e_molar_mass

    name = species.split("_")[0]
    name = name.split(',')[0]
    f = Formula(name)

    if "all_iso" in species:
        return f.mass
    
    return f.isotope.massnumber

def calc_MMW(abundances):
    """
    calc_MMW
    Calculate the mean molecular weight in each layer.

    Args:
        abundances : dict
            dictionary of abundance arrays, each array must have the shape of the pressure array used in pRT,
            and contain the abundance at each layer in the atmosphere.
    """
    mmw = sys.float_info.min  # prevent division by 0

    for key in abundances.keys():
        # exo_k resolution
        spec = key.split("_")[0]
        mmw += abundances[key] / getMM(spec)

    return 1.0 / mmw

class Parameter:

    def __init__(self, name, value=None, prior=(0,1), is_free=True):
        self.name = name
        self.is_free = is_free
        self.value = value
        self.prior = prior

    def set_value(self, value):
        self.value = value

class Retrieval:

    def __init__(self, retrieval_name, out_dir) -> None:
        self.retrieval_name = retrieval_name
        self.out_dir = out_dir
        self.prefix = self.out_dir+'/'+self.retrieval_name+'_'
        if not os.path.exists(self.out_dir):
            os.path.mkdir(self.out_dir)
        self.obs = {} 
        self.params = {}
        self.model_tellu = lambda x: np.ones_like(x)
    
    def free_PT(self):
        p_ret = np.copy(self.press)
        t_names = [x for x in self.params if x.split('_')[0]=='t']
        t_names.sort(reverse=True)
        knots_t = [self.params[x].value for x in t_names]
        knots_p = np.logspace(np.log10(self.press[0]),np.log10(self.press[-1]), len(knots_t))
        t_spline = splrep(np.log10(knots_p), knots_t, k=1)
        tret = splev(np.log10(p_ret), t_spline, der=0)
        t_smooth = gaussian_filter(tret, 1.5)
        if self.debug:
            plt.plot(t_smooth, self.press)
            plt.ylim([self.press[-1],self.press[0]])
            plt.yscale('log')
            plt.xlabel('T (K)')
            plt.ylabel('Pressure (bar)')
            plt.show()
            plt.clf()
        return t_smooth
    
    def add_observation(self, obs: dict = None):
        self.detector_bin = {}
        for instrument in obs:
            self.obs[instrument] = obs[instrument]
            obs[instrument].make_wlen_bins()
            if instrument.lower() == 'crires':
                self.detector_bin[instrument] = 3
            else:
                self.detector_bin[instrument] = 1

    def add_parameter(self, name, value=None, prior=(0,1), is_free=True):
        self.params[name] = Parameter(name, value, prior, is_free)
        print(f"Add parameter - {name}, value: {value}, prior: {prior}, is_free: {is_free}")
        
    def add_free_PT_model(self, t0_prior):
        for i in range(self.N_t_knots):
            self.add_parameter(f't_{i:02}')
        self.params['t_00'].prior = t0_prior
        self.PT_model = self.free_PT




    def add_pRT_objects(self,
                       line_species=['H2O_high', 'CO_high',
                                     'CO_36_high', 'Na'], 
                       rayleigh_species=['H2', 'He'], 
                       continuum_opacities=['H2-H2', 'H2-He'],
                       cloud_species=None,
                       mode='lbl',
                       lbl_opacity_sampling=5,
                       ):
        if cloud_species is not None:
            do_scat_emis = True
        else:
            do_scat_emis = False

        self.pRT_object = {}
        for instrument in self.obs.keys():
            data_object = self.obs[instrument]
            self.pRT_object[instrument] = []
            for i in range(0, data_object.Nchip, self.detector_bin[instrument]):
                wave_tmp = data_object.wlen[i:i+self.detector_bin[instrument]].flatten()
                # set pRT wavelength range sparing 200 pixels 
                # beyond the data wavelengths for each order
                dw = (wave_tmp[1]-wave_tmp[0])*200.
                wlen_cut = [(wave_tmp[0]-dw)*1e-3, (wave_tmp[-1]+dw)*1e-3]
                rt_object = Radtrans(
                        line_species=line_species,
                        rayleigh_species=rayleigh_species,
                        continuum_opacities=continuum_opacities,
                        cloud_species=cloud_species,
                        mode=mode,
                        wlen_bords_micron=wlen_cut,
                        lbl_opacity_sampling=lbl_opacity_sampling,
                        do_scat_emis=do_scat_emis,
                    )
                rt_object.setup_opa_structure(self.press)
                self.pRT_object[instrument].append(rt_object)
        
    
    def add_telluric_model(self, line_species=['H2O', 'CH4'],
                           tellu_grid_path=None,
                           ):
        
        if self.fit_telluric:
            self.add_parameter('airmass', value=1., is_free=False)
            self.add_parameter('tellu_temp', prior=(-9e-4, 9e-4))
            for species in line_species:
                param_name = "tellu_" + species.split('_')[0]
                if species == 'H2O':
                    self.add_parameter(param_name, prior=(0.05, 0.9))
                else:
                    self.add_parameter(param_name, prior=(0.8, 1.2))
            if tellu_grid_path is not None:
                self.grid_h2o = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_h2o.fits'))
                self.grid_ch4 = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_ch4.fits'))
                self.y_n2o = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_n2o.fits'))
                self.y_co = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_co.fits'))
                self.y_co2 = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_co2.fits'))
                self.y_o3 = fits.getdata(os.path.join(tellu_grid_path, 'telfit_grid_o3.fits'))
                self.w_tellu_native = np.genfromtxt(os.path.join(tellu_grid_path, 'telfit_wave.dat'))
            else:
                raise Exception("Please specify the path to the telluric grid")

    def add_free_chem_model(self):
        self.line_species = []
        for instrument in self.obs.keys():
            self.line_species += self.pRT_object[instrument][0].line_species
        for species in self.line_species:
            if '36' in species.split('_'):
                param_name = "logX_" + species.split('_')[0] + "_36"
            else:
                param_name = "logX_" + species.split('_')[0]
            self.add_parameter(param_name, prior=(-12, -2))
        self.add_parameter('H2', is_free=False)
        self.add_parameter('He', is_free=False)

    def add_equ_chem_model(self):
        pass
    def add_cloud_model(self):
        pass
    
        
    def add_GP(self, GP_chip_bin=None, prior_amp=(0,1), prior_tau=(0,1)):
        if self.fit_GP:
            if GP_chip_bin is None: #use one kernel for all orders
                for instrument in self.obs.keys():
                    self.add_parameter(f"GP_{instrument}_amp", prior=prior_amp)
                    self.add_parameter(f"GP_{instrument}_tau", prior=prior_tau)
            else: #different kernel for each chip
                for i in range(0, self.obs[instrument].Nchip, GP_chip_bin):
                    self.add_parameter(f"GP_{instrument}_amp_{i:02}", prior=prior_amp)
                    self.add_parameter(f"GP_{instrument}_tau_{i:02}", prior=prior_tau)
        

    def forward_model_pRT(self):

        # get temperarure profile
        temp = self.PT_model()

        # get abundances and mean molecular weight
        abundances = {}
        for species in self.line_species:
            if '36' in species.split('_'):
                param_name = "logX_" + species.split('_')[0] + "_36"
            else:
                param_name = "logX_" + species.split('_')[0]
            abundances[species] = np.ones_like(self.press) * 1e1 ** self.params[param_name].value

        sum_masses = 0
        for species in abundances.keys():
            sum_masses += abundances[species][0]
        massH2He = 1. - sum_masses
        abundances['H2'] = 2.*0.84/(4*0.16+2*0.84) * massH2He * np.ones_like(temp)
        abundances['He'] = 4.*0.16/(4*0.16+2*0.84) * massH2He * np.ones_like(temp)

        MMW = calc_MMW(abundances)*np.ones_like(temp)

        param_name_radius = "radius"
        param_name_distance = "distance"
        self.model_native = {}
        for instrument in self.obs.keys():
            model = []
            for rt_object in self.pRT_object[instrument]:
                rt_object.calc_flux(temp,
                        abundances,
                        1e1**self.params['logg'].value,
                        MMW,
                        # contribution=contribution,
                        )
                # convert flux f_nu to f_lambda in unit of W/cm**2/um
                f_lambda = rt_object.flux*rt_object.freq**2./nc.c * 1e-7
                wlen_nm = nc.c/rt_object.freq/1e-7
                if param_name_radius in self.params:
                    f_lambda *= (self.params[param_name_radius].value * nc.r_jup / self.params[param_name_distance].value / nc.pc)**2
                model.append([wlen_nm, f_lambda])
            self.model_native[instrument] = model

                           
    def forward_model_telluric(self):
        # interpolate telluric grid
        rel_h2o_range = np.array([0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]) 
        rel_ch4_range = np.array([0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2]) 
        rel_temp_range = np.arange(-0.0009, 0.0012, 3e-4)
        y_h2o = RegularGridInterpolator((rel_temp_range, rel_h2o_range), 
                            self.grid_h2o, bounds_error=False, fill_value=None)(
                            [self.params['tellu_temp'].value, self.params['tellu_H2O'].value])[0]
        y_ch4 = RegularGridInterpolator((rel_temp_range, rel_ch4_range), 
                            self.grid_ch4, bounds_error=False, fill_value=None)(
                            [self.params['tellu_temp'].value, self.params['tellu_CH4'].value])[0]
        y_h2o[y_h2o<0.] = 0.
        y_ch4[y_ch4<0.] = 0.
        tellu_native = (y_h2o*y_ch4*self.y_n2o*self.y_o3*self.y_co*self.y_co2)**(self.params['airmass'].value)
        self.model_tellu = interp1d(self.w_tellu_native, tellu_native, bounds_error=False, fill_value='extrapolate')
        
        # if self.debug:
        #     plt.plot(self.w_tellu_native, tellu_native)
        #     plt.show()

    def plot_model_debug(self, model):
        for instrument in self.obs.keys():
            for dt in model[instrument]:
                wave_tmp, flux_tmp = dt[0], dt[1]
                plt.plot(wave_tmp, flux_tmp, 'k')
        plt.show()

    def plot_rebin_model_debug(self, model):
        for instrument in self.obs.keys():
            for i, flux_tmp in enumerate(model[instrument]):
                wave_tmp = self.obs[instrument].wlen[i]
                flux_obs = self.obs[instrument].flux[i]
                err_obs = self.obs[instrument].err[i]
                plt.errorbar(wave_tmp, flux_obs, err_obs, color='r')
                plt.plot(wave_tmp, flux_tmp, color='k', zorder=10)
        plt.show()

            
    def add_system_params(self, distance=None, limb=0.5):
        self.add_parameter("distance", value=distance, is_free=False)
        self.add_parameter("limb", value=limb, is_free=False)
        self.add_parameter("vsini", prior=(1, 50))
        self.add_parameter("vsys", prior=(-50, 50))
        self.add_parameter("logg", prior=(3.0, 6.0))

    def apply_rot_broaden_rv_shift(self):
        self.model_spin = {}
        for instrument in self.obs.keys():
            model = self.model_native[instrument]
            model_tmp = []
            for dt in model:
                wave_tmp, flux_tmp = dt[0], dt[1]
                wave_shift = wave_tmp * (1. + self.params["vsys"].value*1e5 / nc.c) 
                wlen_up = np.linspace(wave_tmp[0], wave_tmp[-1], len(wave_tmp)*20)

                flux_take = interp1d(wave_shift, flux_tmp, bounds_error=False, fill_value='extrapolate')(wlen_up)
                flux_spin = pyasl.fastRotBroad(wlen_up, flux_take, self.params["limb"].value, self.params["vsini"].value)
                model_tmp.append([wlen_up, flux_spin])
            self.model_spin[instrument] = model_tmp
        
        

    def add_instrument_kernel(self):
        for instrument in self.obs.keys():
            self.add_parameter(f'{instrument}_G', prior=(0.3e5, 2e5))
            self.add_parameter(f'{instrument}_L', prior=(0.1, 5))

    def apply_instrument_broaden(self):
        self.model_convolved = {}
        for instrument in self.obs.keys():
            inst_G = self.params[f'{instrument}_G'].value
            inst_L = self.params[f'{instrument}_L'].value
            model_target = self.model_spin[instrument]
            model_tmp = []
            for dt in model_target:
                wave_tmp, flux_tmp = dt[0], dt[1]
                flux_full = flux_tmp * self.model_tellu(wave_tmp)
                flux_conv = su.SpecConvolve_GL(wave_tmp, flux_full, inst_G, inst_L)
                model_tmp.append([wave_tmp, flux_conv])
            self.model_convolved[instrument] = model_tmp


    def apply_rebin_to_obs_wlen(self):
        self.model_rebin = {}
        for instrument in self.obs.keys():
            model_target = self.model_convolved[instrument]
            obs_target = self.obs[instrument]
            model_tmp = []
            for i in range(obs_target.wlen.shape[0]):
                flux_rebin = rgw.rebin_give_width(model_target[i//self.detector_bin[instrument]][0], 
                                                  model_target[i//self.detector_bin[instrument]][1],
                                                  obs_target.wlen[i], 
                                                  obs_target.wlen_bins[i],
                                                 )
                model_tmp.append(flux_rebin)
            self.model_rebin[instrument] = np.array(model_tmp)

        if self.debug:
            # self.plot_model_debug(self.model_native)
            # self.plot_model_debug(self.model_spin)
            # self.plot_model_debug(self.model_convolved)
            self.plot_rebin_model_debug(self.model_rebin)

    def add_poly_model(self): #or spline?
        if self.fit_poly:
            for instrument in self.obs.keys():
                for i in range(self.obs[instrument].Nchip):
                    for o in range(1, self.fit_poly+1):
                        self.add_parameter(f'poly_{instrument}_{o}_{i:02}', prior=(-5e-2/o, 5e-2/o))
        

    def apply_poly_continuum(self):
        # self.model_cont = {}
        for instrument in self.obs.keys():
            model_target = self.model_rebin[instrument]
            obs_target = self.obs[instrument]
            model_tmp = []
            for i, y_model in enumerate(model_target):
                x = obs_target.wlen[i]
                # y = obs_target.flux[i]

                if self.fit_poly:
                    # correct for the slope or higher order poly of the continuum
                    poly = [1.] 
                    for o in range(1, self.fit_poly+1):
                        poly.append(self.params[f'poly_{instrument}_{o}_{i:02}'].value)
                    y_poly = Chev.chebval((x - np.mean(x))/(np.mean(x)-x[0]), poly)
                    y_model *= y_poly
                    # plt.plot(x, y_poly)

                model_tmp.append(y_model)
            # plt.show()
            self.model_rebin[instrument] = np.array(model_tmp)

        # if self.debug:
        #     self.plot_rebin_model_debug(self.model_rebin)

    def calc_scaling(self, y_model, y_data, y_cov):
        if y_cov.ndim == 2:
            # sparse Cholesky decomposition
            cov_chol = cholesky(y_cov)
            # Scale the model flux to minimize the chi-squared
            lhs = np.dot(y_model, cov_chol.solve_A(y_model))
            rhs = np.dot(y_model, cov_chol.solve_A(y_data))
            f_det = rhs/lhs
            
        elif y_cov.ndim == 1:
            # Scale the model flux to minimize the chi-squared
            lhs = np.dot(y_model, 1/y_cov**2 * y_model)
            rhs = np.dot(y_model, 1/y_cov**2 * y_data)
            f_det = rhs/lhs
        return f_det
    
    def calc_err_infaltion(self, y_model, y_data, y_cov):
        if y_cov.ndim == 2:
            cov_chol = cholesky(y_cov)
            chi_squared = np.dot((y_data-y_model), cov_chol.solve_A(y_data-y_model))
        elif y_cov.ndim == 1:
            chi_squared = np.sum(((y_data-y_model)/y_cov)**2)
            print(chi_squared)
        return np.sqrt(chi_squared/len(y_data))

   
    def calc_logL(self, y_model, y_data, y_cov):
        if y_cov.ndim == 2:
            cov_chol = cholesky(y_cov)
            # Compute the chi-squared error
            chi_squared = np.dot((y_data-y_model), cov_chol.solve_A(y_data-y_model))

            # Log of the determinant (avoids under/overflow issues)
            logdet_cov = cov_chol.logdet()

        elif y_cov.ndim == 1:
            chi_squared = np.sum(((y_data-y_model)/y_cov)**2)

            # Log of the determinant
            logdet_cov = np.sum(np.log(y_cov**2))
        # print(chi_squared, logdet_cov)

        return -(len(y_data)/2*np.log(2*np.pi)+chi_squared+logdet_cov)/2.



    def prior(self, cube, ndim, nparams):
        i = 0
        indices = []
        for key in self.params:
            if self.params[key].is_free:
                a, b = self.params[key].prior
                cube[i] = a+(b-a)*cube[i]
                if key == 't_00':
                    t_i = cube[i]
                elif key.split("_")[0] == 't':
                    indices.append(i)
                i += 1

        if self.PT_is_free:
            # enforce decreasing temperatures from bottom to top layers 
            for k in indices:
                t_i = t_i * cube[k] #(1.-0.5*cube[k])
                cube[k] = t_i


    def loglike(self, cube, ndim, nparams):
        log_likelihood = 0. 

        # draw parameter values from cube
        i_p = 0 # parameter count
        for pp in self.params:
            if self.params[pp].is_free:
                self.params[pp].set_value(cube[i_p])
                i_p += 1
                if self.debug:
                    print(f"{i_p} \t {pp} \t {self.params[pp].value}")


        self.forward_model_pRT()
        if self.fit_telluric:
            self.forward_model_telluric()
        self.apply_rot_broaden_rv_shift()
        self.apply_instrument_broaden()
        self.apply_rebin_to_obs_wlen()
        if self.fit_poly:
            self.apply_poly_continuum()

        self.flux_scaling, self.err_infaltion = {}, {}
        for instrument in self.obs.keys():
            model_target = self.model_rebin[instrument]
            obs_target = self.obs[instrument]._copy()

            if self.fit_GP:
                amp = [self.params[key].value for key in self.params \
                                    if "amp" in key.split("_") and \
                                       instrument in key.split("_")]
                tau = [self.params[key].value for key in self.params \
                                    if "tau" in key.split("_") and \
                                       instrument in key.split("_")]

                obs_target.make_covariance(amp, tau)
                cov = obs_target.cov
            else:
                cov = obs_target.err

            model_tmp, f_dets, betas = [], [], []
            for i, y_model in enumerate(model_target):
                y_data = obs_target.flux[i]

                if self.fit_scaling:
                    f_det = self.calc_scaling(y_model, y_data, cov[i])
                    # Apply the scaling factor to the model
                    y_model *= f_det
                    f_dets.append(f_det)
                model_tmp.append(y_model)

                if self.fit_err_inflation:
                    beta = self.calc_err_infaltion(y_model, y_data, cov[i])
                    cov[i] *= beta
                    betas.append(beta)

                # Add to the log-likelihood
                log_likelihood += self.calc_logL(y_model, y_data, cov[i])
            self.model_rebin[instrument] = np.array(model_tmp)
            self.flux_scaling[instrument] = np.array(f_dets)
            self.err_infaltion[instrument] = np.array(betas)
        
        print(log_likelihood)
        if self.debug:
            print(self.flux_scaling, self.err_infaltion)
            # input("Press Enter to continue...")
            self.plot_rebin_model_debug(self.model_rebin)

        return log_likelihood


    def setup(self, 
              obs,
              line_species,
              press=None,
              N_t_knots=None, 
              t0_prior=None,
              fit_GP=False, 
              fit_poly=1, 
              fit_scaling=True, 
              fit_err_inflation=False,
              fit_telluric=False, 
              tellu_grid_path=None,
              ):
        if press is None:
            self.press = np.logspace(-5,1,50)
        else:
            self.press = press
        self.fit_GP = fit_GP
        self.fit_poly = fit_poly
        self.fit_scaling = fit_scaling
        self.fit_err_inflation = fit_err_inflation
        self.fit_telluric = fit_telluric
        self.add_observation(obs)
        assert self.obs, "No input observations provided"

        print("Creating pRT objects for input data...")
        self.add_pRT_objects(line_species=line_species)
        self.add_system_params()
        self.add_instrument_kernel()

        if N_t_knots is not None and t0_prior is not None:
            self.N_t_knots = N_t_knots
            self.PT_is_free = True
            self.add_free_PT_model(t0_prior=t0_prior)

        self.add_free_chem_model()
        self.add_telluric_model(tellu_grid_path=tellu_grid_path)
        self.add_poly_model()
        self.add_GP()

        # count number of params
        self.n_params = 0
        for x in self.params:
            if self.params[x].is_free:
                self.n_params += 1
        print(f"{self.n_params} free parameters in total.")


    def run(self, n_live_points=500, debug=False):
        self.debug = debug

        pymultinest.run(self.loglike,
            self.prior,
            self.n_params,
            outputfiles_basename=self.prefix,
            resume = False, 
            verbose = True, 
            const_efficiency_mode = True, 
            sampling_efficiency = 0.05,
            n_live_points = n_live_points)
        

    def print_params_info(self, values):
        name_params = [self.params[x].name for x in self.params if self.params[x].is_free]
        for name, value in zip(name_params, values):
            print(f"{name}: \t\t {value}")

    def best_fit_model(self, which='median'):
        a = pymultinest.Analyzer(n_params=self.n_params, outputfiles_basename=self.prefix)
        s = a.get_stats()
        best_fit = s['modes'][0][which]
        self.print_params_info(best_fit[:self.n_params])
        logl = self.loglike(best_fit[:self.n_params], self.n_params, self.n_params)
        # self.model_scaled