import numpy as np
import random
import pandas as pd
import matplotlib.pyplot as plt
from typing import Union
from sklearn.cluster import KMeans
from sklearn.covariance import EmpiricalCovariance, MinCovDet
from scipy.linalg import eigh

from astropy.coordinates import ICRS, LSR, GalacticLSR, CartesianRepresentation
import astropy.units as u

from DistantSigMA.DistantSigMA.PlotlyResults import plot


class SimulateCluster(object):

    def __init__(self, region_data: pd.DataFrame, group_id: Union[str, float],
                 clustering_features: Union[list, np.array], label_column: str = "rsc", cov_split: list = None,
                 multiplier_fraction: float = 1.5):

        self.data = region_data[region_data[label_column] == group_id]
        self.data["distance"] = 1000 / self.data["parallax"]
        self.group_id = group_id
        self.rv = self.data["radial_velocity"].dropna()
        # self.rv_error = self.data["radial_velocity_error"].dropna()

        # clean rv
        # df_cleaned_rv = self.data[self.data['radial_velocity_error'] < 2]
        # self.cleaned_rv = df_cleaned_rv[["radial_velocity", "radial_velocity_error"]].dropna()

        self.cluster_features = clustering_features
        self.cluster_solution = self.data[self.cluster_features]
        self.n_stars = int(self.data.shape[0])
        self.n_samples = int(self.n_stars * multiplier_fraction)
        self.cov_matrix = None
        self.center = np.empty(shape=(len(clustering_features),))

        self.build_covariance_matrix(cov_split)

        self.simulated_points = np.random.multivariate_normal(self.center, self.cov_matrix, self.n_samples)
        self.e_convolved_points = np.empty(shape=(self.n_samples, 8))
        self.mean_dist = np.sqrt(self.center[0] ** 2 + self.center[1] ** 2 + self.center[2] ** 2)

    def build_covariance_matrix(self, custom_split: list = None):

        if not custom_split:
            # split the clustering solution into velocity and position space
            pos_split = self.cluster_solution[["X", "Y", "Z"]]
            vel_split = self.cluster_solution[["v_a_lsr", "v_d_lsr"]]

        else:
            pos_split = self.cluster_solution[custom_split]
            vel_split = self.cluster_solution.loc[:, ~self.cluster_solution.columns.isin(custom_split)]

        # Calculate the empirical covariance matrix
        # cov_estimator = EmpiricalCovariance()
        cov_estimator = MinCovDet()
        pos_cov = cov_estimator.fit(pos_split).covariance_
        vel_cov = cov_estimator.fit(vel_split).covariance_

        # Calculate the position-space eigenvalues, eigenvectors and cluster center
        eigenvalues, eigenvectors = eigh(pos_cov)
        center = cov_estimator.fit(self.cluster_solution).location_

        # fuse new covariance matrix for positional space with the one for velocity space
        cov_matrix = np.zeros(shape=(5, 5))
        np.fill_diagonal(cov_matrix, np.median(eigenvalues))
        cov_matrix[3:, 3:] = vel_cov

        self.cov_matrix = cov_matrix
        self.center = center

    def add_mini_cluster(self, n_members, center_coords, pos_cov_frac):
        cov = self.cov_matrix
        cov[:3, :3] *= pos_cov_frac

        self.cov_matrix = cov
        # center = center_coords
        self.simulated_points = np.random.multivariate_normal(center_coords, cov, n_members)
        self.mean_dist = np.sqrt(center_coords[0] ** 2 + center_coords[1] ** 2 + center_coords[2] ** 2)
        self.n_samples = n_members

    def sample_errors(self, coord_to_sample, sampling_data, sample_radius):
        # in the .loc[x, :] attribute, the entire sampling_data dataframe is masked based on whether the calculated
        # distance is within 4 pc of the cluster center
        # from the masked array, #n_sample errors are sampled from the specified column

        return sampling_data.loc[(sampling_data["dist"] >= self.mean_dist - sample_radius) &
                                 (sampling_data["dist"] <= self.mean_dist + sample_radius), coord_to_sample].sample(
            self.n_samples).values

    def error_convolve(self, sampling_data, sample_radius, return_coords=True):

        ra, dec, dist = self.galacticLSR2spherical(self.simulated_points)
        parallax = 1000 / dist

        # Sample errors from the data
        ra_errors = self.sample_errors(coord_to_sample='ra_error', sampling_data=sampling_data,
                                       sample_radius=sample_radius)
        dec_errors = self.sample_errors(coord_to_sample='dec_error', sampling_data=sampling_data,
                                        sample_radius=sample_radius)
        parallax_errors = self.sample_errors(coord_to_sample='parallax_error', sampling_data=sampling_data,
                                             sample_radius=sample_radius)

        # pmra_errors = self.sample_errors(coord_to_sample='pmra_error', sampling_data=sampling_data)
        # pmdec_errors = self.sample_errors(coord_to_sample='pmdec_error', sampling_data=sampling_data)

        # Resample points
        # TODO: Add to mask if outside range
        ra_resampled = np.random.normal(loc=ra, scale=ra_errors, size=ra.shape[0])
        # TODO: Add to mask if outside range
        dec_resampled = np.random.normal(loc=dec, scale=dec_errors, size=ra.shape[0])
        #
        plx_resampled = np.random.normal(loc=parallax, scale=parallax_errors, size=ra.shape[0])

        # define v_coords
        v_lsr = self.simulated_points[:, 3:]

        # filter out negative parallax values and their ra / dec / velocity counterparts
        positive_plx_mask = plx_resampled > 0

        if not np.all(positive_plx_mask):
            count_false = np.count_nonzero(positive_plx_mask == False)
            print("Negative values encountered in resampled parallax column."
                  f"Removed {count_false} samples.")
            plx_resampled = plx_resampled[positive_plx_mask]
            ra_resampled = ra_resampled[positive_plx_mask]
            dec_resampled = dec_resampled[positive_plx_mask]
            v_lsr = v_lsr[positive_plx_mask]

        # build the table that will be exported for further processing
        cartesian_coords = self.spherical2GalacticLSR([ra_resampled, dec_resampled, 1000 / plx_resampled])
        self.e_convolved_points = np.vstack([self.lsr2icrs([ra_resampled, dec_resampled, plx_resampled], v_lsr),
                                             cartesian_coords]).T

        # Treat errors independently
        if return_coords:
            return np.vstack([ra_resampled, dec_resampled, plx_resampled]).T

    @staticmethod
    def lsr2icrs(positions, lsr_velocities, rv=0):

        ra, dec, plx = positions
        d = 1000 / plx

        pma_lsr = (lsr_velocities[:, 0] * 1000) / (4.74047 * d)
        pmd_lsr = (lsr_velocities[:, 1] * 1000) / (4.74047 * d)

        # Create a CartesianRepresentation object
        lsr = LSR(ra=ra * u.deg, dec=dec * u.deg, distance=d * u.pc, pm_ra=pma_lsr * u.mas / u.yr,
                  pm_dec=pmd_lsr * u.mas / u.yr, radial_velocity=rv * u.km / u.s, representation_type="spherical")

        icrs = lsr.transform_to(ICRS())

        return [icrs.ra.value, icrs.dec.value, 1000 / icrs.distance.value, icrs.pm_ra_cosdec.value, icrs.pm_dec.value]

    @staticmethod
    def galacticLSR2spherical(cartesian_data):

        X, Y, Z, v_a_lsr, v_d_lsr = cartesian_data.T

        c = GalacticLSR(
            x=X * u.pc, y=Y * u.pc, z=Z * u.pc,
            # v_x=U * u.km / u.s, v_y=V * u.km / u.s, v_z=W * u.km / u.s,
            representation_type=CartesianRepresentation,
            # differential_type=CartesianDifferential
        )
        c.representation_type = 'spherical'
        d = c.transform_to(ICRS())

        return [d.ra.value, d.dec.value, d.distance.value]

    @staticmethod
    def spherical2GalacticLSR(spherical_data):

        coord = ICRS(
            ra=spherical_data[0] * u.deg, dec=spherical_data[1] * u.deg, distance=spherical_data[2] * u.pc,
        )
        d = coord.transform_to(GalacticLSR())
        d.representation_type = 'cartesian'

        return [d.x.value, d.y.value, d.z.value]

    @staticmethod
    def Knuths_rule(column_data):
        bin_size = 2.0 * np.std(column_data) * len(column_data) ** (-1 / 3)
        num_bins = int((np.max(column_data) - np.min(column_data)) / bin_size)
        return num_bins

    def diff_histogram(self, input_data):
        # Create a figure with three subplots in a row
        fig, ax = plt.subplots(2, 3, figsize=(10, 7))

        # Iterate over subplots and plot histograms for each column
        for i, label in enumerate(["ra", "dec", "parallax"]):
            # Extract column data from self.data and input_data
            data_column = self.data[label]
            input_column = input_data[:, i]

            # Calculate the number of bins using Knuth's rule
            num_bins_data = self.Knuths_rule(data_column)
            num_bins_input = self.Knuths_rule(input_column)

            # Plot histograms for the original data
            ax[0, i].hist(data_column, bins=num_bins_data, edgecolor='black')
            ax[0, i].set_title(f"{label}_old")

            # Plot histograms for the resampled data
            ax[1, i].hist(input_column, bins=num_bins_input, facecolor="orange", edgecolor='black')
            ax[1, i].set_title(f"{label}_resampled")

        # Adjust layout for better spacing
        plt.suptitle(f"Group {self.group_id} ({self.n_samples})")
        plt.tight_layout()

        return fig


# load data needed for sampling the errors --> no longer needed for the moment
def slim_sampling_data(input_file="Gaia_DR3_500pc_rs.csv", output_file="Gaia_DR3_500pc_10percent.csv",
                       path: str = "/Users/alena/PycharmProjects/SigMA_Orion/Data/", cols=None,
                       slim_factor: float = 0.01):
    if cols is None:
        cols = ["ra_error", "dec_error", "parallax_error", "parallax"]

    gaia_dr3_path = path + input_file
    error_sampling_df = pd.read_csv(gaia_dr3_path, usecols=cols,
                                    skiprows=lambda x: x > 0 and random.random() > slim_factor)
    error_sampling_df["dist"] = 1000 / error_sampling_df["parallax"]

    error_sampling_df.to_csv(path + output_file)


def calculate_std_devs(input_df, SigMA_dict, sampling_data, n_artificial: int = 1, sample_radius: int = 5,
                       output_path: str = None, plot_figs: bool = False):
    df_clusters = input_df[input_df.rsc != -1]  # -1 == field stars

    # Add some small artificial clusters to increase scale factor sensitivity for those
    n_simulated_clusters = len(np.unique(df_clusters["rsc"])) + n_artificial

    # calculate center of all clusters
    cluster_features = ['X', 'Y', 'Z', 'v_a_lsr', "v_d_lsr"]
    kmeans = KMeans(n_clusters=1).fit(df_clusters[cluster_features])
    centers_real = kmeans.cluster_centers_[0]

    # initialize results arrays for the 5 cluster features
    stds = np.empty(shape=(5, n_simulated_clusters))
    e_convolved_dfs = []

    # Loop over the clusters
    for group in np.unique(df_clusters["rsc"])[:]:

        # define subset for length check
        subset = df_clusters[df_clusters["rsc"] == group]

        # Simulate the cluster from its covariance matrix and convolve it with Gaia errors
        sim = SimulateCluster(region_data=df_clusters, group_id=group, clustering_features=cluster_features)
        e_convolved_cluster = sim.error_convolve(sampling_data=sampling_data, sample_radius=sample_radius)
        sim_df = pd.DataFrame(data=sim.e_convolved_points,
                              columns=["ra", "dec", "parallax", "pmra", "pmdec", "X", "Y", "Z"]) \
            .assign(label=int(group))
        e_convolved_dfs.append(sim_df)

        # std cols
        std_columns = ["ra", "dec", "parallax", "pmra", "pmdec"]
        stds[:, group] = sim_df[std_columns].std().values

        # use smallest subset to add a tiny cluster in the center
        if len(subset) == df_clusters.groupby("rsc").size().min():
            sim.add_mini_cluster(n_members=max(SigMA_dict['KNN_list']), center_coords=centers_real,
                                 pos_cov_frac=0.5)
            e_conv_tiny = sim.error_convolve(sampling_data=sampling_data, sample_radius=sample_radius)
            tiny_df = pd.DataFrame(data=sim.e_convolved_points,
                                   columns=["ra", "dec", "parallax", "pmra", "pmdec", "X", "Y", "Z"]).assign(
                label=n_simulated_clusters)
            e_convolved_dfs.append(tiny_df)

            # also add this to the std cols
            stds[:, n_simulated_clusters - 1] = tiny_df[std_columns].std().values

        # resampled data histograms
        if plot_figs:
            fig = sim.diff_histogram(e_convolved_cluster)
            plt.savefig(output_path + f"Group_{sim.group_id}.pdf", dpi=300)

    # Create a master df of all groups
    convolved_df = pd.concat(e_convolved_dfs, ignore_index=True)

    if plot_figs:
        convolved_df.to_csv(output_path + "Simulated_clusters.csv")
        im_clusters = plot(convolved_df["label"], convolved_df, f"Simulated_clusters.html", icrs=True,
                           output_pathname=output_path)

        # outer histogram of the group stds
        outer_fig, ax = plt.subplots(2, 3, figsize=(7, 4))
        ax = ax.ravel()

        try:
            for i, label in enumerate(["ra", "dec", "parallax", "pmra", "pmdec"]):
                data_column = stds[i, :]
                num_bins_data = sim.Knuths_rule(data_column)
                ax[i].hist(data_column, bins=len(data_column), facecolor="green", edgecolor='black')
                ax[i].set_title(f"{label} ({round(min(data_column), 3)}, {round(max(data_column), 3)})")

            plt.suptitle(f"Outer hist")
            plt.tight_layout()
            plt.close()
        except ValueError:
            pass

        outer_fig.savefig(output_path + "Outer_std_distributions.png", dpi=300)

    return stds
# if __name__ == "__main__":
# slim_sampling_data(output_file="Gaia_DR3_500pc_10percent.csv", slim_factor=0.1)
