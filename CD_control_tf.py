#%%
# a minimial implementation of a discrete version of grape
# consisting of a sequence of conditional displacement,
# displacement, and rotation pulses, with
# tuneable parameters

import numpy as np
import tensorflow as tf

#%%
import CD_control.tf_quantum as tfq
import matplotlib.pyplot as plt
from CD_control.helper_functions import plot_wigner
import qutip as qt
from datetime import datetime

#%%
class CD_control_tf:

    # a block is defined as the unitary: CD(beta)D(alpha)R_phi(theta)
    def __init__(
        self,
        initial_state=None,
        target_state=None,
        target_unitary=None,
        P_cav=None,
        N_blocks=1,
        betas=None,
        phis=None,
        thetas=None,
        max_alpha=5,
        max_beta=5,
        saving_directory=None,
        name="CD_control",
        term_fid=0.999,
        beta_penalty_multiplier=0,
        use_displacements=True,
        unitary_optimization=False,
        no_CD_end=True,
        optimize_expectation=False,
        O=None,
    ):
        self.initial_state = initial_state if initial_state is not None else None
        self.initial_state = (
            tfq.qt2tf(initial_state)
            if not tf.is_tensor(self.initial_state)
            else self.initial_state
        )

        self.target_state = target_state if target_state is not None else None
        self.target_state = (
            tfq.qt2tf(target_state)
            if not tf.is_tensor(self.target_state)
            else self.target_state
        )

        self.target_unitary = target_unitary if target_unitary is not None else None
        self.target_unitary = (
            tfq.qt2tf(target_unitary)
            if not tf.is_tensor(self.target_unitary)
            else self.target_unitary
        )

        self.unitary_optimization = unitary_optimization
        self.N_blocks = N_blocks

        self.set_tf_vars(betas=betas, phis=phis, thetas=thetas)

        self.max_alpha = max_alpha if use_displacements else 0.0
        self.max_beta = max_beta
        self.saving_directory = saving_directory
        self.name = name
        self.term_fid = term_fid
        self.beta_penalty_multiplier = beta_penalty_multiplier
        self.use_displacements = use_displacements
        self.no_CD_end = no_CD_end
        self.optimize_expectation = optimize_expectation
        if self.optimize_expectation:
            self.O = tfq.qt2tf(O)

        # todo: handle case when initial state is a tf object.
        if unitary_optimization:
            self.N_cav = self.target_unitary.numpy().shape[0] // 2
        else:
            self.N_cav = self.initial_state.numpy().shape[0] // 2

        self.P_cav = P_cav if P_cav is not None else self.N_cav

        with tf.device("CPU:0"):
            self.a = tfq.destroy(self.N_cav)
            self.adag = tfq.create(self.N_cav)
            self.q = tfq.position(self.N_cav)
            self.p = tfq.momentum(self.N_cav)
            self.n = tfq.num(self.N_cav)
            self.I = tfq.qt2tf(qt.tensor(qt.identity(2), qt.identity(self.N_cav)))

            partial_I = np.array(qt.identity(self.N_cav))
            for j in range(self.P_cav, self.N_cav):
                partial_I[j, j] = 0
            partial_I = qt.Qobj(partial_I)
            self.P_matrix = tfq.qt2tf(qt.tensor(qt.identity(2), partial_I))

            # Pre-diagonalize
            (self._eig_q, self._U_q) = tf.linalg.eigh(self.q)
            (self._eig_p, self._U_p) = tf.linalg.eigh(self.p)
            (self._eig_n, self._U_n) = tf.linalg.eigh(self.n)

            self._qp_comm = tf.linalg.diag_part(self.q @ self.p - self.p @ self.q)

    @tf.function
    def construct_displacement_operators(self, alphas_rho, alphas_angle):

        # Reshape amplitudes for broadcast against diagonals
        sqrt2 = tf.math.sqrt(tf.constant(2, dtype=tf.float32))
        cosines = tf.math.cos(alphas_angle)
        sines = tf.math.sin(alphas_angle)
        re_a = tf.cast(
            tf.reshape(sqrt2 * alphas_rho * cosines, [alphas_rho.shape[0], 1]),
            dtype=tf.complex64,
        )
        im_a = tf.cast(
            tf.reshape(sqrt2 * alphas_rho * sines, [alphas_rho.shape[0], 1]),
            dtype=tf.complex64,
        )

        # Exponentiate diagonal matrices
        expm_q = tf.linalg.diag(tf.math.exp(1j * im_a * self._eig_q))
        expm_p = tf.linalg.diag(tf.math.exp(-1j * re_a * self._eig_p))
        expm_c = tf.linalg.diag(tf.math.exp(-0.5 * re_a * im_a * self._qp_comm))

        # Apply Baker-Campbell-Hausdorff
        return tf.cast(
            self._U_q
            @ expm_q
            @ tf.linalg.adjoint(self._U_q)
            @ self._U_p
            @ expm_p
            @ tf.linalg.adjoint(self._U_p)
            @ expm_c,
            dtype=tf.complex64,
        )

    @tf.function
    def construct_block_operators(self, betas_rho, betas_angle, phis, thetas):

        Bs_g_rho = betas_rho / tf.constant(2, dtype=tf.float32)
        Bs_g_angle = betas_angle
        ds = self.construct_displacement_operators(Bs_g_rho, Bs_g_angle)
        ds_dag = tf.linalg.adjoint(ds)
        Phis = phis - tf.constant(np.pi, dtype=tf.float32) / tf.constant(
            2, dtype=tf.float32
        )
        Thetas = thetas / tf.constant(2, dtype=tf.float32)
        Phis = tf.cast(tfq.matrix_flatten(Phis), dtype=tf.complex64)
        Thetas = tf.cast(tfq.matrix_flatten(Thetas), dtype=tf.complex64)

        exp = tf.math.exp(tf.constant(1j, dtype=tf.complex64) * Phis)
        exp_dag = tf.linalg.adjoint(exp)
        cos = tf.math.cos(Thetas)
        sin = tf.math.sin(Thetas)

        # constructing the blocks of the matrix
        ul = cos * ds
        ll = exp * sin * ds_dag
        ur = tf.constant(-1, dtype=tf.complex64) * exp_dag * sin * ds
        lr = cos * ds_dag

        # without pi pulse, block matrix is:
        # (ul, ur)
        # (ll, lr)
        # however, with pi pulse included:
        # (ll, lr)
        # (ul, ur)
        blocks = -1j * tf.concat([tf.concat([ll, lr], 2), tf.concat([ul, ur], 2)], 1)

        return blocks

    @tf.function
    def state(self, i=0, betas_rho=None, betas_angle=None, phis=None, thetas=None):
        betas_rho = self.betas_rho if betas_rho is None else betas_rho
        betas_angle = self.betas_angle if betas_angle is None else betas_angle
        phis = self.phis if phis is None else phis
        thetas = self.thetas if thetas is None else thetas
        bs = self.construct_block_operators(betas_rho, betas_angle, phis, thetas)
        psi = self.initial_state
        for U in bs[:i]:
            psi = U @ psi
        return psi

    @tf.function
    def final_state(
        self, betas_rho=None, betas_angle=None, phis=None, thetas=None,
    ):
        betas_rho = betas_rho if betas_rho is not None else self.betas_rho
        betas_angle = betas_angle if betas_angle is not None else self.betas_angle
        phis = phis if phis is not None else self.phis
        thetas = thetas if thetas is not None else self.thetas

        bs = self.construct_block_operators(betas_rho, betas_angle, phis, thetas)
        psi = self.initial_state
        for U in bs:
            psi = U @ psi
        return psi

    # TODO: use tf.einsum to quickly do these contractions
    @tf.function
    def state_overlap(self, betas_rho, betas_angle, phis, thetas):
        psif = self.final_state(betas_rho, betas_angle, phis, thetas)
        psi_target_dag = tf.linalg.adjoint(self.target_state)
        overlap = psi_target_dag @ psif
        return overlap

    @tf.function
    def state_fidelity(self, betas_rho, betas_angle, phis, thetas):
        overlap = self.state_overlap(betas_rho, betas_angle, phis, thetas)
        fid = tf.cast(overlap * tf.math.conj(overlap), dtype=tf.float32)
        return fid

    @tf.function
    def mult_bin_tf(self, a):
        while a.shape[0] > 1:
            if a.shape[0] % 2 == 1:
                a = tf.concat(
                    [a[:-2], [tf.matmul(a[-2], a[-1])]], 0
                )  # maybe there's a faster way to deal with immutable constants
            a = tf.matmul(a[::2, ...], a[1::2, ...])
        return a[0]

    @tf.function
    def U_tot(self, betas_rho, betas_angle, phis, thetas):
        bs = self.construct_block_operators(betas_rho, betas_angle, phis, thetas)
        # U_c = tf.scan(lambda a, b: tf.matmul(b, a), bs)[-1]
        U_c = self.mult_bin_tf(
            tf.reverse(bs, axis=[0])
        )  # [U_1,U_2,..] -> [U_N,U_{N-1},..]-> U_N @ U_{N-1} @ .. @ U_1
        # U_c = self.I
        # for U in bs:
        #     U_c = U @ U_c
        return U_c

    @tf.function
    def unitary_fidelity(self, betas_rho, betas_angle, phis, thetas):
        U_circuit = self.U_tot(betas_rho, betas_angle, phis, thetas)
        D = self.P_cav * 2
        overlap = tf.linalg.trace(
            tf.linalg.adjoint(self.target_unitary) @ self.P_matrix @ U_circuit
        )
        return tf.cast(
            (1.0 / D) ** 2 * overlap * tf.math.conj(overlap), dtype=tf.float32
        )

    def set_unitary_fidelity_state_basis(self, states):
        self.initial_unitary_states = states
        self.target_unitary_states = self.target_unitary @ states  # using broadcasting

    def unitary_fidelity_state_decomp(self, betas_rho, betas_angle, phis, thetas):
        bs = self.construct_block_operators(betas_rho, betas_angle, phis, thetas)
        psis = self.initial_unitary_states
        for U in bs:
            psis = U @ psis  # using broadcasting
        psis_target_dag = tf.linalg.adjoint(self.target_unitary_states)
        overlaps = psis_target_dag @ psis
        overlap = tf.reduce_sum(overlaps)
        return tf.cast(
            (1.0 / len(psis)) ** 2 * overlap * tf.math.conj(overlap), dtype=tf.float32
        )

    # returns <psi_f | O | psi_f>
    @tf.function
    def expectation_value(self, betas_rho, betas_angle, phis, thetas, O):
        psif = self.final_state(betas_rho, betas_angle, phis, thetas)
        psif_dag = tf.linalg.adjoint(psif)
        expect = psif_dag @ O @ psif
        return expect

    def plot_initial_state(
        self, contour=True, fig=None, ax=None, max_alpha=6, cbar=False
    ):
        state = tfq.tf2qt(self.initial_state)
        plot_wigner(
            state, contour=contour, fig=fig, ax=ax, max_alpha=max_alpha, cbar=cbar
        )

    def plot_final_state(
        self, contour=True, fig=None, ax=None, max_alpha=6, cbar=False
    ):
        state = tfq.tf2qt(
            self.final_state(self.betas_rho, self.betas_angle, self.phis, self.thetas)
        )
        plot_wigner(
            state, contour=contour, fig=fig, ax=ax, max_alpha=max_alpha, cbar=cbar,
        )

    def plot_target_state(
        self, contour=True, fig=None, ax=None, max_alpha=6, cbar=False
    ):
        state = tfq.tf2qt(self.target_state)
        plot_wigner(
            state, contour=contour, fig=fig, ax=ax, max_alpha=max_alpha, cbar=cbar,
        )

    def plot_state(self, i=0, contour=True, fig=None, ax=None, max_alpha=6, cbar=False):
        state = tfq.tf2qt(self.state(i=i))
        plot_wigner(
            state, contour=contour, fig=fig, ax=ax, max_alpha=max_alpha, cbar=cbar,
        )

    def optimize(
        self,
        learning_rate=0.01,
        epoch_size=100,
        epochs=100,
        dloss_stop=1e-6,
        beta_mask=None,
        phi_mask=None,
        theta_mask=None,
        callback_fun=None,
    ):
        optimizer = tf.optimizers.Adam(learning_rate)
        variables = [self.betas_rho, self.betas_angle, self.phis, self.thetas]

        if beta_mask is None:
            beta_mask = np.ones(self.N_blocks)
            if self.no_CD_end:
                beta_mask[-1] = 0  # don't optimize final CD

        if phi_mask is None:
            phi_mask = np.ones(self.N_blocks)
            phi_mask[0] = 0  # stop gradient on first phi entry

        if theta_mask is None:
            theta_mask = np.ones(self.N_blocks)

        beta_mask = tf.constant(beta_mask, dtype=tf.float32)
        phi_mask = tf.constant(phi_mask, dtype=tf.float32)
        theta_mask = tf.constant(theta_mask, dtype=tf.float32)

        @tf.function
        def entry_stop_gradients(target, mask):
            mask_h = tf.abs(mask - 1)
            return tf.stop_gradient(mask_h * target) + mask * target

        if self.optimize_expectation:

            @tf.function
            def loss_fun(betas_rho, betas_angle, phis, thetas):
                expect = self.expectation_value(
                    betas_rho, betas_angle, phis, thetas, self.O
                )
                return tf.math.log(1 - tf.math.real(expect))

        else:

            @tf.function
            def loss_fun(betas_rho, betas_angle, phis, thetas):
                fid_func = (
                    self.state_fidelity
                    if not self.unitary_optimization
                    else (
                        self.unitary_fidelity_state_decomp
                        if self.unitary_optimization == "states"
                        else self.unitary_fidelity
                    )
                )

                fid = fid_func(betas_rho, betas_angle, phis, thetas)
                return tf.math.log(1 - fid)

        term_loss = np.log(1 - self.term_fid)

        # format of callback will always be
        # callback_fun(self, loss, dloss, epoch)
        # passing self to callback will allow one to print any values of the variables
        if callback_fun is None:

            def callback_fun(obj, loss, dloss, epoch):
                fid = 1 - np.exp(loss)
                print(
                    "Epoch: %d Fid: %.6f Loss: %.6f dLoss: %.6f"
                    % (epoch, fid, loss, dloss)
                )

        initial_loss = loss_fun(
            self.betas_rho, self.betas_angle, self.phis, self.thetas
        )
        print(initial_loss.numpy())
        callback_fun(self, initial_loss.numpy(), 0, 0)

        losses = []
        losses.append(initial_loss.numpy())
        loss = initial_loss
        for epoch in range(epochs + 1)[1:]:
            for _ in range(epoch_size):
                with tf.GradientTape() as tape:
                    betas_rho = entry_stop_gradients(self.betas_rho, beta_mask)
                    betas_angle = entry_stop_gradients(self.betas_angle, beta_mask)
                    phis = entry_stop_gradients(self.phis, phi_mask)
                    thetas = entry_stop_gradients(self.thetas, theta_mask)
                    new_loss = loss_fun(betas_rho, betas_angle, phis, thetas)
                    dloss_dvar = tape.gradient(new_loss, variables)
                optimizer.apply_gradients(zip(dloss_dvar, variables))
            dloss = new_loss - loss
            loss = new_loss
            losses.append(loss.numpy())
            callback_fun(self, loss.numpy(), dloss.numpy(), epoch)
            if loss <= term_loss:
                self.normalize_angles()
                self.print_info()
                return losses
            # TODO: use real gradient here, not the dloss!
            if np.abs(dloss) < dloss_stop:
                self.normalize_angles()
                self.print_info()
                return losses
        self.normalize_angles()
        self.print_info()
        return losses

    # TODO: update for tf
    def randomize(self, beta_scale=None, alpha_scale=None):
        beta_scale = self.max_beta if beta_scale is None else beta_scale
        alpha_scale = self.max_alpha if alpha_scale is None else alpha_scale
        ang_beta = np.random.uniform(-np.pi, np.pi, self.N_blocks)
        rho_beta = np.random.uniform(0, beta_scale, self.N_blocks)
        ang_alpha = np.random.uniform(-np.pi, np.pi, self.N_blocks)
        rho_alpha = np.random.uniform(0, alpha_scale, self.N_blocks)
        phis = np.random.uniform(-np.pi, np.pi, self.N_blocks)
        thetas = np.random.uniform(-np.pi, np.pi, self.N_blocks)

        phis[0] = 0  # optimization is done realative to first phi
        if self.no_CD_end:
            rho_beta[-1] = 0

        self.betas_rho = tf.Variable(rho_beta, dtype=tf.float32, trainable=True)
        self.betas_angle = tf.Variable(ang_beta, dtype=tf.float32, trainable=True)
        self.alphas_rho = tf.Variable(rho_alpha, dtype=tf.float32, trainable=True)
        self.alphas_angle = tf.Variable(ang_alpha, dtype=tf.float32, trainable=True)
        self.phis = tf.Variable(phis, dtype=tf.float32, trainable=True)
        self.thetas = tf.Variable(thetas, dtype=tf.float32, trainable=True)

    def get_numpy_vars(self, betas_rho=None, betas_angle=None, phis=None, thetas=None):
        betas_rho = self.betas_rho if betas_rho is None else betas_rho
        betas_angle = self.betas_angle if betas_angle is None else betas_angle
        phis = self.phis if phis is None else phis
        thetas = self.thetas if thetas is None else thetas

        betas = betas_rho.numpy() * np.exp(1j * betas_angle.numpy())
        phis = phis.numpy()
        thetas = thetas.numpy()

        return betas, phis, thetas

    def set_tf_vars(self, betas=None, phis=None, thetas=None):
        # if None, set to zero
        self.betas_rho = (
            tf.Variable(np.abs(np.array(betas)), dtype=tf.float32, trainable=True)
            if betas is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )
        self.betas_angle = (
            tf.Variable(np.angle(np.array(betas)), dtype=tf.float32, trainable=True)
            if betas is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )
        """
        self.alphas_rho = (
            tf.Variable(np.abs(np.array(self.alphas)), dtype=tf.float32, trainable=True)
            if alphas is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )
        self.alphas_angle = (
            tf.Variable(np.angle(np.array(self.alphas)), dtype=tf.float32, trainable=True)
            if alphas is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )
        """
        self.phis = (
            tf.Variable(phis, dtype=tf.float32, trainable=True)
            if phis is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )
        self.thetas = (
            tf.Variable(thetas, dtype=tf.float32, trainable=True)
            if phis is not None
            else tf.Variable(tf.zeros(self.N_blocks, dtype=tf.float32), trainable=True)
        )

    # when parameters is specificed, normalize_angles is being used
    # during the optimization. In this case, it normalizes the parameters
    # and returns the parameters without updating self.
    # if parameters not specified, just normalize self's angles.
    # todo: make faster with numpy...
    def normalize_angles(self):
        betas, phis, thetas = self.get_numpy_vars()
        thetas_new = []
        for theta in thetas:
            while theta < -np.pi:
                theta = theta + 2 * np.pi
            while theta > np.pi:
                theta = theta - 2 * np.pi
            thetas_new.append(theta)
        thetas = np.array(thetas_new)
        phis_new = []
        for phi in phis:
            while phi < -np.pi:
                phi = phi + 2 * np.pi
            while phi > np.pi:
                phi = phi - 2 * np.pi
            phis_new.append(phi)
        phis = np.array(phis_new)
        self.set_tf_vars(betas, phis, thetas)

    def save(self):
        datestr = datetime.now().strftime("%Y%m%d_%H_%M_%S")
        filestring = self.saving_directory + self.name + "_" + datestr
        filename_np = filestring + ".npz"
        filename_qt = filestring + ".qt"
        betas, phis, thetas = self.get_numpy_vars(betas_rho, betas_angle, phis, thetas)
        np.savez(
            filename_np,
            betas=betas,
            phis=phis,
            thetas=thetas,
            max_alpha=self.max_alpha,
            max_beta=self.max_beta,
            name=self.name,
            circuits=self.circuits,
        )
        print("\n\nparameters saved as: " + filename_np)
        qt.qsave([self.initial_state, self.target_state], filename_qt)
        print("states saved as: " + filename_qt)
        self.print_info()
        # print('name for loading:' + filestring)
        return filestring

    def load(self, filestring):
        filename_np = filestring + ".npz"
        filename_qt = filestring + ".qt"
        f = np.load(filename_np)
        betas, alphas, phis, thetas, max_alpha, max_beta, name = (
            f["betas"],
            f["alphas"],
            f["phis"],
            f["thetas"],
            f["max_alpha"],
            f["max_beta"],
            str(f["name"]),
        )
        circuits = f["circuits"] if ["circuits"] in f else []
        print("loaded parameters from:" + filename_np)
        f.close()
        states = qt.qload(filename_qt)
        initial_state, target_state = states[0], states[1]
        print("loaded states from:" + filename_qt)
        self.__init__(
            initial_state=initial_state,
            target_state=target_state,
            N_blocks=len(betas),
            betas=betas,
            alphas=alphas,
            phis=phis,
            thetas=thetas,
            max_alpha=max_alpha,
            max_beta=max_beta,
            name=name,
            circuits=circuits,
        )
        self.print_info()

    def print_info(
        self, betas_rho=None, betas_angle=None, phis=None, thetas=None, human=True
    ):
        betas_rho = self.betas_rho if betas_rho is None else betas_rho
        betas_angle = self.betas_angle if betas_angle is None else betas_angle
        phis = self.phis if phis is None else phis
        thetas = self.thetas if thetas is None else thetas
        f = (
            self.state_fidelity(betas_rho, betas_angle, phis, thetas)
            if not self.unitary_optimization
            else (
                self.unitary_fidelity_state_decomp(betas_rho, betas_angle, phis, thetas)
                if self.unitary_optimization == "states"
                else self.unitary_fidelity(betas_rho, betas_angle, phis, thetas)
            )
        )
        betas, phis, thetas = self.get_numpy_vars(betas_rho, betas_angle, phis, thetas)
        if human:
            with np.printoptions(precision=5, suppress=True):
                print("\n\n" + str(self.name))
                print("N_blocks:     " + str(self.N_blocks))
                print("betas:        " + str(betas))
                # print("alphas:       " + str(alphas))
                print("phis (deg):   " + str(phis * 180.0 / np.pi))
                print("thetas (deg): " + str(thetas * 180.0 / np.pi))
                print("Fidelity:     %.5f" % f)
                print("\n")
        else:
            print("\n\n" + str(self.name))
            print("N_blocks: " + repr(self.N_blocks))
            print("betas: " + repr(betas))
            # print("alphas: " + repr(alphas))
            print("phis: " + repr(phis))
            print("thetas: " + repr(thetas))
            print("Fidelity: " + repr(f))
            print("\n")


# %%
