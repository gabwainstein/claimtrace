# Third-party data and produced-work notices

Claimtrace's original software and documentation are licensed under the repository's MIT license.
That license does not relicense third-party datasets or outputs derived from them. The public demos
carry the following separate terms and attribution.

## Palmer Penguins demo

The CSV files under `examples/penguin_study/data/` originate from `palmerpenguins` v0.1.0:

- Horst AM, Hill AP, Gorman KB (2020), *palmerpenguins* v0.1.0,
  <https://doi.org/10.5281/zenodo.3960218>.
- Source license: [CC0 1.0](https://allisonhorst.github.io/palmerpenguins/LICENSE.html).

The source and transformed CSVs remain under their upstream CC0 terms. Their citation and content
hashes are retained for scientific provenance.

## PhysioNet EEG Motor Movement/Imagery demo

The files under `examples/eegbci_study/data/`, `examples/eegbci_study/results/`, and
`examples/eegbci_study/figures/` include a derived dataset and produced works generated from the
EEG Motor Movement/Imagery Dataset v1.0.0.

Contains information from the [EEG Motor Movement/Imagery Dataset v1.0.0](https://physionet.org/content/eegmmidb/1.0.0/),
which is made available under the [Open Data Commons Attribution License v1.0](https://physionet.org/content/eegmmidb/view-license/1.0.0/).

The raw EDF files are not committed. Reusers of the checked-in prepared epochs, result artifacts,
or figure should preserve this notice and comply with the upstream license. Requested scientific
citations are:

- Schalk G. (2009). *EEG Motor Movement/Imagery Dataset* (version 1.0.0). PhysioNet.
  <https://doi.org/10.13026/C28G6P>.
- Schalk G, McFarland DJ, Hinterberger T, Birbaumer N, Wolpaw JR (2004). BCI2000: A
  General-Purpose Brain-Computer Interface (BCI) System. *IEEE Transactions on Biomedical
  Engineering*, 51(6), 1034-1043. <https://doi.org/10.1109/TBME.2004.827072>.
- Goldberger AL, Amaral LAN, Glass L, Hausdorff JM, Ivanov PC, Mark RG, Mietus JE, Moody GB,
  Peng C-K, Stanley HE (2000). PhysioBank, PhysioToolkit, and PhysioNet: Components of a New
  Research Resource for Complex Physiologic Signals. *Circulation*, 101(23), e215-e220.
  <https://doi.org/10.1161/01.CIR.101.23.E215>.

This notice is provided for attribution and license clarity, not as legal advice.
